"""Direct OpenAI Responses transport behind the typed generation boundary."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import aclosing
from typing import Any

import httpx2
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, omit
from openai.types.responses import Response

from app.config import SUPPORTED_OPENAI_CHAT_MODELS, SUPPORTED_OPENAI_FLEX_MODELS
from app.errors import ErrorCode, tag_error
from app.metrics import metrics_collector
from app.observability.provider_events import record_provider_exception
from app.providers.base import AIResponse, BaseAIProvider, is_openai_model
from app.providers.request_factory import generation_request_from_history
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingMode,
    GroundingReport,
    GroundingSource,
    ImagePart,
    KeyDisposition,
    PromptRole,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    ServiceTier,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    ThinkingLevel,
    TokenUsage,
    VisibleTextBuffer,
)
from app.providers.typed_payloads import _base64_image
from app.request_context import get_request_id

_sdk_clients: dict[str, AsyncOpenAI] = {}
_MODELS = SUPPORTED_OPENAI_CHAT_MODELS
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class UnsupportedResponsesRequest(ValueError):
    """A local capability mismatch; retrying another key cannot repair it."""


def _get_sdk_client(api_key: str) -> AsyncOpenAI:
    identity = hashlib.sha256(api_key.encode()).hexdigest()
    client = _sdk_clients.get(identity)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1", max_retries=0, timeout=120.0)
        _sdk_clients[identity] = client
    return client


async def close_http_clients() -> None:
    clients = list(_sdk_clients.values())
    _sdk_clients.clear()
    results = await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
    if any(isinstance(result, BaseException) for result in results):
        raise RuntimeError("OpenAI SDK transport cleanup failed")


async def responses_payload(request: GenerationRequest, model_name: str) -> dict[str, Any]:
    """Reject unsupported input instead of silently dropping or stringifying it."""
    model = model_name.removeprefix("openai-direct/")
    if not is_openai_model(model_name) or model not in _MODELS:
        raise UnsupportedResponsesRequest("Unsupported direct OpenAI model")
    if request.service_tier is ServiceTier.FLEX and model not in SUPPORTED_OPENAI_FLEX_MODELS:
        raise UnsupportedResponsesRequest("This direct OpenAI model does not support Flex processing")
    inputs: list[dict[str, Any]] = []
    for turn in request.turns:
        content: list[dict[str, Any]] = []
        for part in turn.parts:
            if isinstance(part, TextPart):
                content.append(
                    {"type": "output_text", "text": part.text, "annotations": []}
                    if turn.role is PromptRole.MODEL
                    else {"type": "input_text", "text": part.text}
                )
            elif isinstance(part, ImagePart):
                if turn.role is PromptRole.MODEL or part.mime_type not in _IMAGE_TYPES:
                    raise UnsupportedResponsesRequest("Unsupported Responses image input")
                image = await _base64_image(part)
                if image is None:
                    raise UnsupportedResponsesRequest("Could not encode Responses image input")
                encoded, mime = image
                content.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"})
            else:
                raise UnsupportedResponsesRequest("Unsupported Responses prompt part")
        inputs.append({"role": "assistant" if turn.role is PromptRole.MODEL else "user", "content": content})
    effort = "low" if model == "gpt-6.1-sol" else "none"
    if request.thinking_level in {ThinkingLevel.LOW, ThinkingLevel.MEDIUM, ThinkingLevel.HIGH}:
        effort = request.thinking_level.value
    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
        "store": False,
        "service_tier": request.service_tier.value,
    }
    if model.startswith("gpt-6"):
        payload["reasoning"] = {"effort": effort}
    elif request.thinking_level in {ThinkingLevel.LOW, ThinkingLevel.MEDIUM, ThinkingLevel.HIGH}:
        raise UnsupportedResponsesRequest("This direct OpenAI model does not support reasoning effort")
    if request.system_instruction:
        payload["instructions"] = request.system_instruction
    if request.grounding in {GroundingMode.PROVIDER_SEARCH, GroundingMode.PROVIDER_SEARCH_REQUIRED}:
        if not model.startswith("gpt-6"):
            raise UnsupportedResponsesRequest("This direct OpenAI model does not support Responses web search")
        payload["tools"] = [{"type": "web_search"}]
        if request.grounding is GroundingMode.PROVIDER_SEARCH_REQUIRED:
            payload["tool_choice"] = {"type": "web_search"}
    return payload


def _usage(response: Response) -> TokenUsage:
    raw = response.usage
    if raw is None:
        return TokenUsage()
    return TokenUsage(
        prompt=raw.input_tokens,
        completion=raw.output_tokens,
        total=raw.total_tokens,
        cached=getattr(raw.input_tokens_details, "cached_tokens", None),
    )


def _grounding(response: Response) -> tuple[GroundingReport, bool]:
    sources: dict[str, GroundingSource] = {}
    queries: list[str] = []
    searched = False
    for item in response.output:
        if item.type == "web_search_call":
            searched |= item.status == "completed"
            query = getattr(item.action, "query", None)
            if isinstance(query, str) and query.strip():
                queries.append(query)
        for part in getattr(item, "content", ()):
            for annotation in getattr(part, "annotations", ()):
                if annotation.type != "url_citation":
                    continue
                url, title = annotation.url, annotation.title
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    sources[url] = GroundingSource(url=url, title=title if isinstance(title, str) and title else url)
    return GroundingReport(sources=tuple(sources.values()), search_queries=tuple(queries)), searched


class OpenAIProvider(BaseAIProvider):
    provider_name = "openai"

    async def stream(self, request: GenerationRequest, *, model_name: str):
        route = RouteUsed(ProviderKind.OPENAI, request.models[0], model_name, request.service_tier)
        visible = False
        buffer = VisibleTextBuffer()

        def failed(code: ErrorCode, diagnostic: str, key=KeyDisposition.UNCHANGED, retry=RetryDisposition.DO_NOT_RETRY):
            return StreamFailed(
                code=code,
                phase=FailurePhase.AFTER_TEXT if visible else FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.DO_NOT_RETRY if visible else retry,
                key=KeyDisposition.UNCHANGED if visible else key,
                diagnostic=diagnostic,
                route=route,
            )

        try:
            async with asyncio.timeout(request.provider_timeout_seconds):
                first_budget = request.first_text_timeout_seconds
                first_deadline = asyncio.get_running_loop().time() + (
                    first_budget if first_budget is not None else 20.0
                )
                async with asyncio.timeout_at(first_deadline):
                    payload = await responses_payload(request, model_name)
                    headers = {}
                    if request_id := get_request_id():
                        headers["X-Client-Request-Id"] = request_id
                    await metrics_collector.record_api_call(self.provider_name, model_name)
                    manager = _get_sdk_client(self.api_key).responses.stream(
                        **payload,
                        text_format=request.response_schema if request.response_schema is not None else omit,
                        extra_headers=headers,
                        timeout=request.provider_timeout_seconds,
                    )
                    stream = await manager.__aenter__()
                terminal: StreamCompleted | StreamFailed | None = None
                try:
                    iterator = stream.__aiter__()
                    while True:
                        try:
                            async with asyncio.timeout_at(None if visible else first_deadline):
                                native_event = await anext(iterator)
                        except StopAsyncIteration:
                            break
                        # The SDK owns framing, event types, and structured output parsing.
                        kind = native_event.type
                        if not isinstance(kind, str):
                            raise ValueError("Invalid Responses event")
                        native_response = getattr(native_event, "response", None)
                        actual_tier = getattr(native_response, "service_tier", None)
                        if isinstance(actual_tier, str) and actual_tier:
                            route = RouteUsed(
                                ProviderKind.OPENAI, request.models[0], model_name, request.service_tier, actual_tier
                            )
                        if kind == "response.output_text.delta":
                            raw_delta = getattr(native_event, "delta", None)
                            if not isinstance(raw_delta, str):
                                raise ValueError("Invalid Responses text delta")
                            if delta := buffer.push(raw_delta):
                                visible = True
                                yield delta
                        elif kind in {"error", "response.failed"}:
                            result = getattr(native_event, "response", None)
                            error = getattr(result, "error", None)
                            error_code = (
                                getattr(error, "code", None)
                                or getattr(error, "type", None)
                                or getattr(native_event, "code", None)
                            )
                            code, key, retry = self._error_disposition(0, error_code)
                            terminal = failed(code, "Responses API reported failure", key, retry)
                            break
                        elif kind in {"response.completed", "response.incomplete"}:
                            result = getattr(native_event, "response", None)
                            if not isinstance(result, Response):
                                raise ValueError("Missing Responses terminal response")
                            if kind == "response.incomplete":
                                terminal = failed(ErrorCode.INVALID_RESPONSE, "Responses generation incomplete")
                                break
                            if result.status != "completed":
                                raise ValueError("Invalid Responses completion status")
                            grounding, searched = _grounding(result)
                            if request.grounding is GroundingMode.PROVIDER_SEARCH_REQUIRED and not searched:
                                terminal = failed(
                                    ErrorCode.INVALID_RESPONSE, "Required Responses web search did not complete"
                                )
                            elif not visible:
                                terminal = failed(
                                    ErrorCode.EMPTY_RESPONSE,
                                    "Responses completed without visible text",
                                    KeyDisposition.TRANSIENT_FAILURE,
                                    RetryDisposition.TRY_NEXT_KEY,
                                )
                            else:
                                actual = result.model
                                terminal_route = RouteUsed(
                                    ProviderKind.OPENAI,
                                    request.models[0],
                                    f"openai-direct/{actual}" if isinstance(actual, str) and actual else model_name,
                                    request.service_tier,
                                    route.actual_service_tier,
                                )
                                terminal = StreamCompleted(
                                    FinishReason.from_raw("stop"), _usage(result), grounding, terminal_route
                                )
                            break
                finally:
                    await manager.__aexit__(None, None, None)
                if terminal is None:
                    raise ValueError("Responses stream ended without terminal event")
                yield terminal
        except asyncio.CancelledError:
            raise
        except TimeoutError, APITimeoutError:
            yield failed(
                ErrorCode.TIMEOUT, "Responses deadline expired", KeyDisposition.UNCHANGED, RetryDisposition.TRY_NEXT_KEY
            )
        except APIStatusError as exc:
            error_code = exc.code if isinstance(exc.code, str) else exc.type if isinstance(exc.type, str) else None
            code, key, retry = self._error_disposition(exc.status_code, error_code)
            yield failed(code, f"Responses HTTP {exc.status_code}", key, retry)
        except UnsupportedResponsesRequest:
            yield failed(ErrorCode.INVALID_REQUEST, "Unsupported Responses request")
        except ValueError, TypeError, KeyError, RuntimeError, AttributeError, IndexError, AssertionError:
            yield failed(ErrorCode.INVALID_RESPONSE, "Invalid or unsupported Responses payload/stream")
        except (APIConnectionError, httpx2.HTTPError) as exc:
            error_id = record_provider_exception(
                exc,
                provider=self.provider_name,
                model=model_name,
                api_key=self.api_key,
                failure_phase=(FailurePhase.AFTER_TEXT if visible else FailurePhase.BEFORE_TEXT).value,
            )
            failure = failed(
                ErrorCode.NETWORK,
                "Responses transport failed",
                KeyDisposition.TRANSIENT_FAILURE,
                RetryDisposition.TRY_NEXT_KEY,
            )
            yield StreamFailed(
                code=failure.code,
                phase=failure.phase,
                retry=failure.retry,
                key=failure.key,
                diagnostic=failure.diagnostic,
                route=route,
                error_id=error_id,
            )
        except APIError as exc:
            error_code = exc.code if isinstance(exc.code, str) else exc.type if isinstance(exc.type, str) else None
            code, key, retry = self._error_disposition(0, error_code)
            yield failed(code, "OpenAI SDK reported Responses failure", key, retry)

    @staticmethod
    def _error_disposition(status: int, code: str | None):
        if code == "resource_unavailable":
            return ErrorCode.OVERLOADED, KeyDisposition.UNCHANGED, RetryDisposition.RETRY_LATER
        if status == 401 or code == "invalid_api_key":
            return ErrorCode.INVALID_KEY, KeyDisposition.INVALID, RetryDisposition.TRY_NEXT_KEY
        if code == "insufficient_quota":
            return ErrorCode.QUOTA_EXCEEDED, KeyDisposition.EXHAUSTED, RetryDisposition.TRY_NEXT_KEY
        if status == 429 or code == "rate_limit_exceeded":
            return ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED, RetryDisposition.TRY_NEXT_KEY
        if status in {400, 403, 404, 422}:
            return ErrorCode.INVALID_REQUEST, KeyDisposition.UNCHANGED, RetryDisposition.DO_NOT_RETRY
        return ErrorCode.OVERLOADED, KeyDisposition.TRANSIENT_FAILURE, RetryDisposition.TRY_NEXT_KEY

    async def get_response(
        self,
        history,
        model_name,
        system_instruction=None,
        user_id=None,
        chat_id=None,
        max_retries=3,
        timeout=120.0,
        thinking_level=None,
    ) -> AIResponse:
        # Rotation belongs to the router; the transport makes exactly one attempt.
        return await self._execute_request(
            history, model_name, system_instruction, user_id, chat_id, timeout, thinking_level
        )

    async def _execute_request(
        self, history, model_name, system_instruction, user_id, chat_id, timeout, thinking_level=None
    ) -> AIResponse:
        text: list[str] = []
        try:
            request = await generation_request_from_history(
                models=(model_name,),
                history=history,
                system_instruction=system_instruction,
                user_id=user_id,
                chat_id=chat_id,
                thinking_level=thinking_level,
                allow_deferred=False,
                allow_key_race=False,
            )
            async with asyncio.timeout(timeout), aclosing(self.stream(request, model_name=model_name)) as events:
                async for event in events:
                    if isinstance(event, TextDelta):
                        text.append(event.text)
                    elif isinstance(event, StreamCompleted):
                        return AIResponse(
                            "".join(text),
                            event.usage.total or 0,
                            True,
                            provider=self.provider_name,
                            model=event.route.actual_model,
                        )
                    elif isinstance(event, StreamFailed):
                        return AIResponse(
                            tag_error(event.code, "❌ OpenAI сейчас недоступен. Попробуйте позже."),
                            0,
                            False,
                            error_message=event.diagnostic,
                            provider=self.provider_name,
                            model=model_name,
                        )
        except TimeoutError:
            return AIResponse(
                tag_error(ErrorCode.TIMEOUT, "⏳ Превышено время ожидания OpenAI."),
                0,
                False,
                error_message="Responses deadline expired",
                provider=self.provider_name,
                model=model_name,
            )
        except ValueError, TypeError:
            pass
        return AIResponse(
            tag_error(ErrorCode.INVALID_REQUEST, "❌ Неподдерживаемый запрос OpenAI."),
            0,
            False,
            error_message="Unsupported Responses request",
            provider=self.provider_name,
            model=model_name,
        )

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        """Typed stream telemetry is owned by the router observer."""

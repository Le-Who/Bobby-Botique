"""Direct OpenAI Responses transport behind the typed generation boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

import httpx

from app.config import SUPPORTED_OPENAI_CHAT_MODELS
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
from app.utils.json_compat import json
from app.utils.network import NetworkErrorHandler

_openai_http_client: httpx.AsyncClient | None = None
_MODELS = SUPPORTED_OPENAI_CHAT_MODELS
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_MAX_EVENT_CHARS = 2_000_000


class UnsupportedResponsesRequest(ValueError):
    """A local capability mismatch; retrying another key cannot repair it."""


async def close_http_clients() -> None:
    global _openai_http_client
    if _openai_http_client is not None:
        await _openai_http_client.aclose()
        _openai_http_client = None


async def responses_payload(request: GenerationRequest, model_name: str) -> dict[str, Any]:
    """Reject unsupported input instead of silently dropping or stringifying it."""
    model = model_name.removeprefix("openai-direct/")
    if not is_openai_model(model_name) or model not in _MODELS:
        raise UnsupportedResponsesRequest("Unsupported direct OpenAI model")
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
        "stream": True,
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


async def _sse_events(response: httpx.Response, deadline: float | None, visible: list[bool]):
    """Read complete SSE frames; comments and reasoning never reset first text."""
    lines = response.aiter_lines()
    data: list[str] = []
    size = 0
    while True:
        try:
            async with asyncio.timeout_at(None if visible[0] else deadline):
                line = await anext(lines)
        except StopAsyncIteration:
            if data:
                raise ValueError("Responses stream ended inside an SSE frame") from None
            return
        if not line:
            if data:
                event = json.loads("\n".join(data))
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("Invalid Responses SSE event")
                yield event
            data = []
            size = 0
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value)
            if size > _MAX_EVENT_CHARS:
                raise ValueError("Responses SSE event exceeds limit")
            data.append(value)


def _usage(response: dict[str, Any]) -> TokenUsage:
    raw = response.get("usage") or {}
    if not isinstance(raw, dict):
        raise ValueError("Invalid Responses usage")
    details = raw.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        raise ValueError("Invalid Responses cached usage")
    return TokenUsage(
        prompt=raw.get("input_tokens"),
        completion=raw.get("output_tokens"),
        total=raw.get("total_tokens"),
        cached=details.get("cached_tokens"),
    )


def _grounding(response: dict[str, Any]) -> tuple[GroundingReport, bool]:
    sources: dict[str, GroundingSource] = {}
    queries: list[str] = []
    searched = False
    output = response.get("output", [])
    if not isinstance(output, list):
        raise ValueError("Invalid Responses output")
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Invalid Responses output item")
        if item.get("type") == "web_search_call":
            searched |= item.get("status") == "completed"
            action = item.get("action") or {}
            if isinstance(action, dict):
                query = action.get("query")
                if isinstance(query, str) and query.strip():
                    queries.append(query)
        content = item.get("content", [])
        if not isinstance(content, list):
            raise ValueError("Invalid Responses output content")
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("Invalid Responses output content")
            annotations = part.get("annotations", [])
            if not isinstance(annotations, list):
                raise ValueError("Invalid Responses annotations")
            for annotation in annotations:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                url, title = annotation.get("url"), annotation.get("title")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    sources[url] = GroundingSource(url=url, title=title if isinstance(title, str) and title else url)
    return GroundingReport(sources=tuple(sources.values()), search_queries=tuple(queries)), searched


class OpenAIProvider(BaseAIProvider):
    provider_name = "openai"

    async def stream(self, request: GenerationRequest, *, model_name: str):
        global _openai_http_client
        route = RouteUsed(ProviderKind.OPENAI, request.models[0], model_name)
        visible = [False]
        buffer = VisibleTextBuffer()

        def failed(code: ErrorCode, diagnostic: str, key=KeyDisposition.UNCHANGED, retry=RetryDisposition.DO_NOT_RETRY):
            return StreamFailed(
                code=code,
                phase=FailurePhase.AFTER_TEXT if visible[0] else FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.DO_NOT_RETRY if visible[0] else retry,
                key=KeyDisposition.UNCHANGED if visible[0] else key,
                diagnostic=diagnostic,
                route=route,
            )

        try:
            # Includes payload encoding, opening headers, SSE metadata and completion.
            async with asyncio.timeout(request.provider_timeout_seconds):
                first_budget = request.first_text_timeout_seconds
                # The direct latency path has a finite first-visible budget even for interactive calls.
                first_deadline = asyncio.get_running_loop().time() + (
                    first_budget if first_budget is not None else 20.0
                )
                async with asyncio.timeout_at(first_deadline):
                    payload = await responses_payload(request, model_name)
                if _openai_http_client is None:
                    _openai_http_client = NetworkErrorHandler.create_robust_http_client()
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                if request_id := get_request_id():
                    headers["X-Client-Request-Id"] = request_id
                await metrics_collector.record_api_call(self.provider_name, model_name)
                async with asyncio.timeout_at(first_deadline):
                    context = _openai_http_client.stream(
                        "POST",
                        "https://api.openai.com/v1/responses",
                        json=payload,
                        headers=headers,
                        timeout=httpx.Timeout(request.provider_timeout_seconds, connect=10.0),
                    )
                    response = await context.__aenter__()
                try:
                    if response.is_error:
                        body = bytearray()
                        async with asyncio.timeout_at(first_deadline):
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk[: _MAX_EVENT_CHARS + 1 - len(body)])
                                if len(body) > _MAX_EVENT_CHARS:
                                    break
                        error_code = None
                        try:
                            parsed = json.loads(body) if len(body) <= _MAX_EVENT_CHARS else None
                            error = parsed.get("error") if isinstance(parsed, dict) else None
                            if isinstance(error, dict) and isinstance(error.get("code"), str):
                                error_code = error["code"]
                        except ValueError:
                            pass
                        code, key, retry = self._error_disposition(response.status_code, error_code)
                        await response.aclose()
                        yield failed(code, f"Responses HTTP {response.status_code}", key, retry)
                        return
                    async with aclosing(_sse_events(response, first_deadline, visible)) as events:
                        async for event in events:
                            kind = event["type"]
                            if kind == "response.output_text.delta":
                                if not isinstance(event.get("delta"), str):
                                    raise ValueError("Invalid Responses text delta")
                                if delta := buffer.push(event["delta"]):
                                    visible[0] = True
                                    yield delta
                            elif kind in {"error", "response.failed"}:
                                failed_response = event.get("response") or {}
                                if not isinstance(failed_response, dict):
                                    raise ValueError("Invalid Responses failure")
                                error = event.get("error") or failed_response.get("error") or {}
                                error_code = error.get("code") if isinstance(error, dict) else None
                                code, key, retry = self._error_disposition(0, error_code)
                                await response.aclose()
                                yield failed(code, "Responses API reported failure", key, retry)
                                return
                            elif kind in {"response.completed", "response.incomplete"}:
                                result = event.get("response")
                                if not isinstance(result, dict):
                                    raise ValueError("Missing Responses terminal response")
                                if kind == "response.incomplete":
                                    await response.aclose()
                                    yield failed(ErrorCode.INVALID_RESPONSE, "Responses generation incomplete")
                                    return
                                if result.get("status") != "completed":
                                    raise ValueError("Invalid Responses completion status")
                                grounding, searched = _grounding(result)
                                if request.grounding is GroundingMode.PROVIDER_SEARCH_REQUIRED and not searched:
                                    await response.aclose()
                                    yield failed(
                                        ErrorCode.INVALID_RESPONSE, "Required Responses web search did not complete"
                                    )
                                    return
                                if not visible[0]:
                                    await response.aclose()
                                    yield failed(
                                        ErrorCode.EMPTY_RESPONSE,
                                        "Responses completed without visible text",
                                        KeyDisposition.TRANSIENT_FAILURE,
                                        RetryDisposition.TRY_NEXT_KEY,
                                    )
                                    return
                                usage = _usage(result)
                                actual = result.get("model")
                                terminal_route = RouteUsed(
                                    ProviderKind.OPENAI,
                                    request.models[0],
                                    f"openai-direct/{actual}" if isinstance(actual, str) and actual else model_name,
                                )
                                # Publish the outcome only after releasing the response. A close
                                # error must become one failure, never a second terminal event.
                                await response.aclose()
                                yield StreamCompleted(FinishReason.from_raw("stop"), usage, grounding, terminal_route)
                                return
                    raise ValueError("Responses stream ended without terminal event")
                finally:
                    await context.__aexit__(None, None, None)
        except asyncio.CancelledError:
            raise
        except TimeoutError, httpx.TimeoutException:
            yield failed(
                ErrorCode.TIMEOUT, "Responses deadline expired", KeyDisposition.UNCHANGED, RetryDisposition.TRY_NEXT_KEY
            )
        except UnsupportedResponsesRequest:
            yield failed(ErrorCode.INVALID_REQUEST, "Unsupported Responses request")
        except ValueError, TypeError, KeyError:
            yield failed(ErrorCode.INVALID_RESPONSE, "Invalid or unsupported Responses payload/stream")
        except httpx.HTTPError as exc:
            error_id = record_provider_exception(
                exc,
                provider=self.provider_name,
                model=model_name,
                api_key=self.api_key,
                failure_phase=(FailurePhase.AFTER_TEXT if visible[0] else FailurePhase.BEFORE_TEXT).value,
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

    @staticmethod
    def _error_disposition(status: int, code: str | None):
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

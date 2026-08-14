"""OpenRouter AI provider — self-contained execution logic."""

import asyncio
import base64
import logging
import time
from typing import Any

import httpx
from google.genai.errors import APIError
from PIL import Image

from app.errors import ErrorCode, tag_error
from app.metrics import metrics_collector
from app.providers.base import AIResponse, BaseAIProvider
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    ProviderKind,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamFailed,
    ThinkingLevel,
    TokenUsage,
    VisibleTextBuffer,
)
from app.providers.typed_payloads import openai_messages
from app.request_context import get_request_id
from app.utils.api_logger import api_logger
from app.utils.image_utils import TaggedImage, save_image_as_bytes
from app.utils.json_compat import json
from app.utils.network import NetworkErrorHandler

# Module-level httpx client for OpenRouter
_openrouter_http_client: httpx.AsyncClient | None = NetworkErrorHandler.create_robust_http_client()


def _optional_int_item(source: dict[str, Any], name: str) -> int | None:
    value = source.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def close_http_clients() -> None:
    """Close module-level HTTP clients on shutdown (prevents resource warnings)."""
    global _openrouter_http_client
    if _openrouter_http_client is not None:
        await _openrouter_http_client.aclose()
        _openrouter_http_client = None
        logging.info("OpenRouter HTTP client closed")


class OpenRouterProvider(BaseAIProvider):
    """OpenRouter AI provider — self-contained execution logic.

    Subclasses may override ``_get_url()``, ``_get_headers()``, and
    ``_strip_model_prefix()`` to adapt this class to other OpenAI-compatible
    endpoints (e.g. OpencodeGoProvider).
    """

    provider_name = "openrouter"

    def _provider_kind(self) -> ProviderKind:
        if self.provider_name == "opencode":
            return ProviderKind.OPENCODE
        if self.provider_name == "freetheai":
            return ProviderKind.FREETHEAI
        return ProviderKind.OPENROUTER

    async def stream(self, request: GenerationRequest, *, model_name: str):
        """Emit typed events from an OpenAI-compatible chat-completions stream."""
        route = RouteUsed(
            provider=self._provider_kind(),
            requested_model=request.models[0],
            actual_model=model_name,
        )
        messages = await openai_messages(request)
        if not messages:
            yield StreamFailed(
                code=ErrorCode.INVALID_REQUEST,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.DO_NOT_RETRY,
                key=KeyDisposition.UNCHANGED,
                diagnostic="OpenAI-compatible message conversion produced no messages",
                route=route,
            )
            return

        client = _openrouter_http_client
        if client is None:
            yield StreamFailed(
                code=ErrorCode.NETWORK,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.RETRY_LATER,
                key=KeyDisposition.UNCHANGED,
                diagnostic="OpenRouter HTTP client is not initialized",
                route=route,
            )
            return

        thinking_level = None
        if request.thinking_level is not None and request.thinking_level is not ThinkingLevel.AUTO:
            thinking_level = request.thinking_level.value
        payload: dict[str, Any] = {
            "model": self._strip_model_prefix(model_name),
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        payload.update(self._extra_payload_params(model_name, thinking_level))

        text_emitted = False
        finish_reason = FinishReason.from_raw(None)
        usage = TokenUsage()
        text_buffer = VisibleTextBuffer()

        try:
            async with client.stream(
                "POST",
                self._get_url(),
                json=payload,
                headers=self._get_headers(),
                timeout=request.provider_timeout_seconds,
            ) as response:
                response.raise_for_status()
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    native_usage = data.get("usage")
                    if isinstance(native_usage, dict):
                        usage = TokenUsage(
                            prompt=_optional_int_item(native_usage, "prompt_tokens"),
                            completion=_optional_int_item(native_usage, "completion_tokens"),
                            total=_optional_int_item(native_usage, "total_tokens"),
                            cached=_optional_int_item(native_usage, "cached_tokens"),
                        )

                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    raw_finish = choice.get("finish_reason")
                    if raw_finish:
                        finish_reason = FinishReason.from_raw(str(raw_finish))
                    text = choice.get("delta", {}).get("content", "")
                    delta = text_buffer.push(text) if isinstance(text, str) else None
                    if delta is not None:
                        text_emitted = True
                        yield delta

        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            await exc.response.aread()
            status = exc.response.status_code
            if status == 429:
                code = ErrorCode.RATE_LIMIT
                key = KeyDisposition.RATE_LIMITED
                retry = RetryDisposition.TRY_NEXT_KEY
            elif status in {401, 403}:
                code = ErrorCode.INVALID_KEY
                key = KeyDisposition.INVALID
                retry = RetryDisposition.TRY_NEXT_KEY
            elif status == 402:
                code = ErrorCode.QUOTA_EXCEEDED
                key = KeyDisposition.EXHAUSTED
                retry = RetryDisposition.TRY_NEXT_KEY
            elif status in {502, 503, 504}:
                code = ErrorCode.OVERLOADED if status == 503 else ErrorCode.NETWORK
                key = KeyDisposition.TRANSIENT_FAILURE
                retry = RetryDisposition.TRY_NEXT_KEY
            elif status == 400:
                code = ErrorCode.INVALID_REQUEST
                key = KeyDisposition.UNCHANGED
                retry = RetryDisposition.DO_NOT_RETRY
            else:
                code = ErrorCode.GENERIC
                key = KeyDisposition.TRANSIENT_FAILURE
                retry = RetryDisposition.TRY_NEXT_KEY
            if text_emitted:
                retry = RetryDisposition.DO_NOT_RETRY
                key = KeyDisposition.UNCHANGED
            yield StreamFailed(
                code=code,
                phase=FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT,
                retry=retry,
                key=key,
                diagnostic=f"HTTP {status}: {exc.response.text[:400]}",
                route=route,
            )
            return
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: {exc}"[:500].replace(self.api_key, "[redacted]")
            yield StreamFailed(
                code=ErrorCode.NETWORK if isinstance(exc, httpx.HTTPError) else ErrorCode.GENERIC,
                phase=FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT,
                retry=(
                    RetryDisposition.DO_NOT_RETRY
                    if text_emitted
                    else RetryDisposition.TRY_NEXT_KEY
                ),
                key=(
                    KeyDisposition.UNCHANGED
                    if text_emitted
                    else KeyDisposition.TRANSIENT_FAILURE
                ),
                diagnostic=diagnostic,
                route=route,
            )
            return

        if not text_emitted:
            yield StreamFailed(
                code=ErrorCode.EMPTY_RESPONSE,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="OpenAI-compatible stream completed without visible text",
                route=route,
            )
            return

        yield StreamCompleted(
            finish_reason=finish_reason,
            usage=usage,
            grounding=GroundingReport(),
            route=route,
        )

    # ── Overridable template methods ─────────────────────────────────────────

    def _get_url(self) -> str:
        """Return the chat completions endpoint URL."""
        return "https://openrouter.ai/api/v1/chat/completions"

    def _get_headers(self) -> dict[str, str]:
        """Return request headers for this provider."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/gemaibotv2",
            "X-Title": "GeminiBot v2",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _strip_model_prefix(self, model_name: str) -> str:
        """Strip any internal routing prefix before sending to the API.

        OpenRouter uses full slugs (e.g. ``stepfun/step-3.5-flash:free``),
        so no stripping is needed here.  Overridden by ``OpencodeGoProvider``
        to remove the ``opencode-go/`` prefix.
        """
        return model_name

    def _extra_payload_params(self, model_name: str, thinking_level: str | None) -> dict:
        """Extra OpenAI-compatible payload params (hook for subclasses).

        Base implementation returns an empty dict.
        OpencodeGoProvider overrides this to inject reasoning_effort for supporting models.
        """
        return {}

    def _build_http_error_tag(
        self,
        status: int,
        response_text: str,
        model_name: str,
    ) -> str:
        """Map provider HTTP errors to tagged user-facing messages."""
        if status == 429:
            return tag_error(
                ErrorCode.RATE_LIMIT,
                "⏱️ Превышен лимит запросов. Подождите немного.",
            )
        if status == 401:
            return tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ. Проверьте настройки.")
        if status == 402:
            return tag_error(ErrorCode.QUOTA_EXCEEDED, "💳 Недостаточно средств на счету OpenRouter.")
        if status == 503:
            return tag_error(
                ErrorCode.OVERLOADED,
                "🔄 Сервер OpenRouter перегружен. Попробуйте позже.",
            )
        return tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {status}")

    async def _execute_request(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        timeout: float,
        thinking_level: str | None = None,
    ) -> AIResponse:
        start_time = None

        try:
            await metrics_collector.record_api_call("openrouter", model_name)
            start_time = time.time()

            # Convert Gemini history → OpenAI format
            messages = await self._build_messages(history, system_instruction)
            if not messages:
                msg = "Failed to create valid messages for OpenRouter API"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_content_creation", msg)
                return AIResponse(
                    text=f"❌ {msg}",
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            # Build request
            url = self._get_url()
            headers = self._get_headers()
            api_model = self._strip_model_prefix(model_name)
            payload: dict = {"model": api_model, "messages": messages}
            payload.update(self._extra_payload_params(model_name, thinking_level))
            logging.debug(
                "%s: sending %d messages to %s (api_model=%s)",
                self.provider_name,
                len(messages),
                model_name,
                api_model,
            )

            # httpx has a 30s read timeout; this is a safety net
            try:
                if _openrouter_http_client is None:
                    raise RuntimeError("OpenRouter HTTP client not initialized")
                response = await asyncio.wait_for(
                    _openrouter_http_client.post(url, json=payload, headers=headers),
                    timeout=90.0,
                )
                response.raise_for_status()
                response_data = response.json()
            except httpx.HTTPStatusError as e:
                return await self._handle_http_error(e, model_name, start_time, user_id, chat_id)
            except TimeoutError:
                msg = f"OpenRouter API request timed out for model {model_name}"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_timeout", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(
                        ErrorCode.TIMEOUT,
                        "⏰ Превышено время ожидания ответа от API. Попробуйте позже.",
                    ),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )
            except (APIError, httpx.HTTPError) as e:
                msg = f"OpenRouter API error: {e!r}"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_api", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {msg}"),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            # Validate response structure
            if not response_data or "choices" not in response_data or not response_data["choices"]:
                msg = "OpenRouter API returned invalid response"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_invalid_response", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(
                        ErrorCode.INVALID_RESPONSE,
                        "❌ API вернул некорректный ответ. Попробуйте еще раз.",
                    ),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            response_text = response_data["choices"][0].get("message", {}).get("content", "")
            if not response_text:
                msg = "OpenRouter API returned empty response"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_empty_response", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(
                        ErrorCode.EMPTY_RESPONSE,
                        "❌ API вернул пустой ответ. Попробуйте еще раз.",
                    ),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            usage_data = response_data.get("usage") or {}
            token_count = usage_data.get("total_tokens", 0)
            cached_tokens = usage_data.get("prompt_tokens_details", {}).get("cached_tokens", 0) or usage_data.get(
                "cache_read_input_tokens", 0
            )
            if cached_tokens:
                logging.debug(
                    "%s prompt cache hit: model=%s cached=%d total=%d",
                    self.provider_name,
                    model_name,
                    cached_tokens,
                    token_count,
                )

            # Log success
            if start_time is not None:
                api_logger.log_response(
                    "openrouter",
                    start_time,
                    model=model_name,
                    response_length=len(response_text),
                    token_count=token_count,
                )

            return AIResponse(
                text=response_text,
                token_count=token_count,
                success=True,
                provider=self.provider_name,
                model=model_name,
            )

        except (APIError, httpx.HTTPError) as e:
            logging.error("OpenRouter API generic error: %s", e, exc_info=True)
            await metrics_collector.record_error("openrouter_api", str(e))
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            return AIResponse(
                text=tag_error(ErrorCode.GENERIC, f"❌ Произошла непредвиденная ошибка API: {e}"),
                token_count=0,
                success=False,
                error_message=str(e),
                provider=self.provider_name,
                model=model_name,
            )

    async def _build_messages(self, history: list, system_instruction: str | None) -> list:
        """Convert Gemini-format history → OpenAI-format messages."""
        messages = []
        if system_instruction:
            content = str(system_instruction).strip()
            if content:
                messages.append({"role": "system", "content": content})

        # Pass 1: Collect tasks for concurrent processing
        image_tasks = []
        for item in history:
            if not isinstance(item, dict):
                continue
            parts = item.get("parts", [])
            if not isinstance(parts, list):
                parts = [parts] if parts is not None else []

            for part in parts:
                if isinstance(part, TaggedImage) and not part.pre_compressed:
                    image_tasks.append(
                        save_image_as_bytes(part.data, cache_key=part.cache_key, task_type=part.task_type)
                    )
                elif isinstance(part, (bytes, bytearray, Image.Image)):
                    image_data = bytes(part) if isinstance(part, bytearray) else part
                    image_tasks.append(save_image_as_bytes(image_data))

        # Execute concurrently
        processed_images = []
        if image_tasks:
            processed_images = await asyncio.gather(*image_tasks, return_exceptions=True)

        # Pass 2: Build actual objects
        img_idx = 0
        for item in history:
            if not isinstance(item, dict):
                continue
            role = item.get("role", "user")
            if role == "model":
                role = "assistant"

            parts = item.get("parts", [])
            if not isinstance(parts, list):
                parts = [parts] if parts is not None else []

            content_parts = []
            for part in parts:
                img_bytes: bytes | None = None

                if isinstance(part, TaggedImage):
                    if part.pre_compressed:
                        img_bytes = part.data
                    else:
                        res = processed_images[img_idx]
                        img_idx += 1
                        img_bytes = res if not isinstance(res, BaseException) else None

                    if img_bytes:
                        img_b64 = await asyncio.to_thread(lambda b=img_bytes: base64.b64encode(b).decode("utf-8"))  # type: ignore[misc]  # lambda default-arg pattern
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                            }
                        )
                elif isinstance(part, (bytes, bytearray, Image.Image)):
                    res = processed_images[img_idx]
                    img_idx += 1
                    img_bytes = res if not isinstance(res, BaseException) else None
                    if img_bytes:
                        img_b64 = await asyncio.to_thread(lambda b=img_bytes: base64.b64encode(b).decode("utf-8"))  # type: ignore[misc]  # lambda default-arg pattern
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                            }
                        )
                else:
                    text = str(part)
                    if text.strip():
                        content_parts.append({"type": "text", "text": text})

            if content_parts:
                if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                    messages.append({"role": role, "content": content_parts[0]["text"]})  # type: ignore[dict-item]  # content is str|list
                else:
                    messages.append({"role": role, "content": content_parts})  # type: ignore[dict-item]  # content is str|list

        return messages

    async def _handle_http_error(
        self,
        e: httpx.HTTPStatusError,
        model: str,
        start_time,
        user_id,
        chat_id,
    ) -> AIResponse:
        msg = f"OpenRouter API HTTP error: {e.response.status_code} - {e.response.text}"
        logging.error(msg)
        await metrics_collector.record_error("openrouter_http", msg)
        self._log_failure(start_time, model, msg, user_id, chat_id)

        status = e.response.status_code
        text = self._build_http_error_tag(status, e.response.text, model)

        return AIResponse(
            text=text,
            token_count=0,
            success=False,
            error_message=msg,
            provider=self.provider_name,
            model=model,
        )

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        if start_time is not None:
            api_logger.log_response(
                "openrouter",
                start_time,
                model=model,
                response_length=0,
                success=False,
                error_message=msg,
            )


def _has_multimodal_content(history: list) -> bool:
    """Detect if history contains multimodal (image) parts."""
    for message in history:
        parts = message.get("parts", [])
        for part in parts:
            if isinstance(part, (Image.Image, TaggedImage)):
                return True
            if isinstance(part, (bytes, bytearray)):
                return True
    return False

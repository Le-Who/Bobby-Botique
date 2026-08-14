"""Opencode Go provider with per-model transport selection.

OpenCode Go exposes a mixed transport surface:
- most models use an OpenAI-compatible ``/chat/completions`` endpoint
- MiniMax M2.5 / M2.7 use an Anthropic-compatible ``/messages`` endpoint

The router still selects one provider class for every ``opencode-go/*`` model,
so this provider must adapt the HTTP shape at request time based on the model.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx
from PIL import Image

from app.errors import ErrorCode, tag_error
from app.providers import openrouter as openrouter_provider
from app.providers.base import AIResponse
from app.providers.openrouter import OpenRouterProvider
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
    TextDelta,
    TokenUsage,
    VisibleTextBuffer,
)
from app.providers.typed_payloads import anthropic_messages_payload
from app.request_context import get_request_id
from app.utils.image_utils import TaggedImage, save_image_as_bytes
from app.utils.json_compat import json


class OpencodeGoProvider(OpenRouterProvider):
    """Opencode Go AI provider.

    Uses standard Bearer auth for OpenAI-compatible models and Anthropic-style
    headers/payloads for MiniMax models routed through ``/v1/messages``.
    """

    provider_name = "opencode"

    _CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"
    _MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"
    _MESSAGES_MODELS = frozenset({"minimax-m2.5", "minimax-m2.7"})
    _MESSAGES_MAX_TOKENS = 8192

    async def stream(self, request: GenerationRequest, *, model_name: str):
        if not self._uses_messages_transport(model_name):
            async for event in super().stream(request, model_name=model_name):
                yield event
            return

        route = RouteUsed(
            provider=ProviderKind.OPENCODE,
            requested_model=request.models[0],
            actual_model=model_name,
        )
        payload = await anthropic_messages_payload(
            request,
            api_model=self._strip_model_prefix(model_name),
            max_tokens=self._MESSAGES_MAX_TOKENS,
        )
        if not payload["messages"]:
            yield StreamFailed(
                code=ErrorCode.INVALID_REQUEST,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.DO_NOT_RETRY,
                key=KeyDisposition.UNCHANGED,
                diagnostic="Opencode message conversion produced no messages",
                route=route,
            )
            return
        payload["stream"] = True

        client = openrouter_provider._openrouter_http_client
        if client is None:
            yield StreamFailed(
                code=ErrorCode.NETWORK,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.RETRY_LATER,
                key=KeyDisposition.UNCHANGED,
                diagnostic="Opencode HTTP client is not initialized",
                route=route,
            )
            return

        text_emitted = False
        finish_reason = FinishReason.from_raw(None)
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        cached_tokens: int | None = None
        text_buffer = VisibleTextBuffer()

        try:
            async with client.stream(
                "POST",
                self._get_url_for_model(model_name),
                json=payload,
                headers=self._get_headers_for_model(model_name),
                timeout=request.provider_timeout_seconds,
            ) as response:
                response.raise_for_status()
                current_event: str | None = None
                data_lines: list[str] = []

                async def _decode_event():
                    nonlocal finish_reason, prompt_tokens, completion_tokens, cached_tokens
                    if not data_lines:
                        return None
                    data_str = "\n".join(data_lines).strip()
                    if not data_str or data_str == "[DONE]":
                        return None
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        return None

                    event_type = current_event or data.get("type")
                    native_usage = data.get("usage")
                    if not isinstance(native_usage, dict):
                        message = data.get("message")
                        native_usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(native_usage, dict):
                        value = native_usage.get("input_tokens")
                        if isinstance(value, int) and not isinstance(value, bool):
                            prompt_tokens = value
                        value = native_usage.get("output_tokens")
                        if isinstance(value, int) and not isinstance(value, bool):
                            completion_tokens = value
                        value = native_usage.get("cache_read_input_tokens")
                        if isinstance(value, int) and not isinstance(value, bool):
                            cached_tokens = value

                    if event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            text = delta.get("text")
                            return text_buffer.push(text) if isinstance(text, str) else None
                    elif event_type == "message_delta":
                        delta = data.get("delta", {})
                        if isinstance(delta, dict) and delta.get("stop_reason"):
                            finish_reason = FinishReason.from_raw(str(delta["stop_reason"]))
                    elif event_type == "error":
                        error = data.get("error", {})
                        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
                        error_type = str(error.get("type", "")) if isinstance(error, dict) else ""
                        lowered = f"{error_type} {message}".lower()
                        if "rate" in lowered or "limit" in lowered:
                            code = ErrorCode.RATE_LIMIT
                            key = KeyDisposition.RATE_LIMITED
                        elif "overload" in lowered:
                            code = ErrorCode.OVERLOADED
                            key = KeyDisposition.TRANSIENT_FAILURE
                        else:
                            code = ErrorCode.GENERIC
                            key = KeyDisposition.TRANSIENT_FAILURE
                        return StreamFailed(
                            code=code,
                            phase=(FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT),
                            retry=(RetryDisposition.DO_NOT_RETRY if text_emitted else RetryDisposition.TRY_NEXT_KEY),
                            key=(KeyDisposition.UNCHANGED if text_emitted else key),
                            diagnostic=f"Opencode SSE error: {error_type} {message}"[:500],
                            route=route,
                        )
                    return None

                async for raw_line in response.aiter_lines():
                    line = raw_line.strip("\r")
                    if line == "":
                        event = await _decode_event()
                        current_event = None
                        data_lines = []
                        if isinstance(event, TextDelta):
                            text_emitted = True
                            yield event
                        elif isinstance(event, StreamFailed):
                            yield event
                            return
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())

                if data_lines:
                    event = await _decode_event()
                    if isinstance(event, TextDelta):
                        text_emitted = True
                        yield event
                    elif isinstance(event, StreamFailed):
                        yield event
                        return

        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            await exc.response.aread()
            status = exc.response.status_code
            code = (
                ErrorCode.RATE_LIMIT
                if status == 429
                else ErrorCode.OVERLOADED
                if status in {502, 503, 504}
                else ErrorCode.INVALID_KEY
                if status in {401, 403}
                else ErrorCode.INVALID_REQUEST
                if status == 400
                else ErrorCode.GENERIC
            )
            key = (
                KeyDisposition.RATE_LIMITED
                if status == 429
                else KeyDisposition.INVALID
                if status in {401, 403}
                else KeyDisposition.TRANSIENT_FAILURE
            )
            yield StreamFailed(
                code=code,
                phase=FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT,
                retry=(RetryDisposition.DO_NOT_RETRY if text_emitted else RetryDisposition.TRY_NEXT_KEY),
                key=KeyDisposition.UNCHANGED if text_emitted else key,
                diagnostic=f"Opencode HTTP {status}: {exc.response.text[:400]}",
                route=route,
            )
            return
        except Exception as exc:
            yield StreamFailed(
                code=ErrorCode.NETWORK if isinstance(exc, httpx.HTTPError) else ErrorCode.GENERIC,
                phase=FailurePhase.AFTER_TEXT if text_emitted else FailurePhase.BEFORE_TEXT,
                retry=(RetryDisposition.DO_NOT_RETRY if text_emitted else RetryDisposition.TRY_NEXT_KEY),
                key=(KeyDisposition.UNCHANGED if text_emitted else KeyDisposition.TRANSIENT_FAILURE),
                diagnostic=f"{type(exc).__name__}: {exc}"[:500].replace(self.api_key, "[redacted]"),
                route=route,
            )
            return

        if not text_emitted:
            yield StreamFailed(
                code=ErrorCode.EMPTY_RESPONSE,
                phase=FailurePhase.BEFORE_TEXT,
                retry=RetryDisposition.TRY_NEXT_KEY,
                key=KeyDisposition.TRANSIENT_FAILURE,
                diagnostic="Opencode messages stream completed without visible text",
                route=route,
            )
            return

        total = (
            prompt_tokens + completion_tokens
            if prompt_tokens is not None and completion_tokens is not None
            else None
        )
        yield StreamCompleted(
            finish_reason=finish_reason,
            usage=TokenUsage(
                prompt=prompt_tokens,
                completion=completion_tokens,
                total=total,
                cached=cached_tokens,
            ),
            grounding=GroundingReport(),
            route=route,
        )

    # Models that support reasoning_effort (OpenAI-compatible extended thinking).
    # DeepSeek V4 Pro/Flash: "low"/"medium"/"high"/"max"
    # Kimi K2.5/K2.6: "low"/"high"
    _REASONING_EFFORT_MODELS = frozenset(
        {
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "kimi-k2.5",
            "kimi-k2.6",
        }
    )

    # Models that support OpenAI-compatible function calling (tools array).
    # All /chat/completions models potentially support tools; we whitelist the
    # ones confirmed or very likely to support it based on model family.
    # MiniMax models use Anthropic /messages transport — handled separately.
    _TOOLS_MODELS = frozenset(
        {
            "glm-5",
            "glm-5.1",
            "kimi-k2.5",
            "kimi-k2.6",
            "mimo-v2-pro",
            "mimo-v2-omni",
            "mimo-v2.5-pro",
            "mimo-v2.5",
            "qwen3.5-plus",
            "qwen3.6-plus",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "big-pickle",
        }
    )
    # Map internal thinking_level labels -> API reasoning_effort values.
    _THINKING_TO_EFFORT: dict[str, str] = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "max": "max",
    }

    def _get_url(self) -> str:
        """Preserve OpenRouter template expectations for chat-completions models."""
        return self._CHAT_URL

    def _get_headers(self) -> dict[str, str]:
        """Preserve OpenRouter template expectations for chat-completions models."""
        return self._build_chat_headers()

    def _build_chat_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _build_messages_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "x-api-key": self.api_key,
            "anthropic-version": self._ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _get_url_for_model(self, model_name: str) -> str:
        if self._uses_messages_transport(model_name):
            return self._MESSAGES_URL
        return self._CHAT_URL

    def _get_headers_for_model(self, model_name: str) -> dict[str, str]:
        if self._uses_messages_transport(model_name):
            return self._build_messages_headers()
        return self._build_chat_headers()

    def _strip_model_prefix(self, model_name: str) -> str:
        """Strip internal ``opencode-go/`` routing prefix before sending to API."""
        return model_name.removeprefix("opencode-go/")

    def _uses_messages_transport(self, model_name: str) -> bool:
        return self._strip_model_prefix(model_name) in self._MESSAGES_MODELS

    def _supports_reasoning_effort(self, model_name: str) -> bool:
        return self._strip_model_prefix(model_name) in self._REASONING_EFFORT_MODELS

    def _supports_tools(self, model_name: str) -> bool:
        """Return True if this model supports OpenAI-compatible function calling."""
        return (
            not self._uses_messages_transport(model_name)
            and self._strip_model_prefix(model_name) in self._TOOLS_MODELS
        )

    def _extra_payload_params(self, model_name: str, thinking_level: str | None) -> dict:
        """Inject reasoning_effort for DeepSeek V4 and Kimi K2 models.
        Also injects tools/tool_choice when set via _pending_tools (from _execute_request
        or typed stream generation).

        Called by OpenRouterProvider._execute_request and typed streaming
        to enrich the payload with model-specific thinking parameters.
        """
        params: dict[str, Any] = {}
        # reasoning_effort
        if thinking_level and self._supports_reasoning_effort(model_name):
            effort = self._THINKING_TO_EFFORT.get(thinking_level)
            if effort:
                params["reasoning_effort"] = effort
        # function calling tools (set by request execution)
        pending_tools = getattr(self, "_pending_tools", None)
        if pending_tools:
            params["tools"] = pending_tools
            pending_tc = getattr(self, "_pending_tool_choice", None)
            if pending_tc:
                params["tool_choice"] = pending_tc
        return params

    def _build_http_error_tag(
        self,
        status: int,
        response_text: str,
        model_name: str,
    ) -> str:
        """Avoid treating model-specific 401/403 responses as broken credentials.

        Opencode can reject a specific model with 401/403 while the same key still
        succeeds on other models. Only explicit invalid-key wording should map
        to INVALID_KEY; model-access failures should stay non-key-related so the
        router can cascade without labeling the key as permanently broken.
        """
        if status in {401, 403}:
            body = (response_text or "").lower()
            invalid_key_markers = (
                "invalid api key",
                "invalid key",
                "api key is invalid",
                "bad api key",
                "bad key",
                "invalid token",
                "token is invalid",
                "authentication failed",
                "auth failed",
            )
            model_access_markers = (
                "model",
                "access",
                "permission",
                "not allowed",
                "not available",
                "unsupported",
                "does not exist",
                "not found",
                "forbidden",
            )
            if any(marker in body for marker in invalid_key_markers):
                return tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ. Проверьте настройки.")
            if any(marker in body for marker in model_access_markers):
                logging.warning(
                    "Opencode rejected model access without invalidating key: model=%s status=%s body=%s",
                    model_name,
                    status,
                    (response_text or "")[:200],
                )
                return tag_error(
                    ErrorCode.INVALID_REQUEST,
                    "❌ Opencode отклонил доступ к этой модели для текущего ключа.",
                )
            logging.warning(
                "Opencode returned ambiguous auth error; treating as model/request access: model=%s status=%s body=%s",
                model_name,
                status,
                (response_text or "")[:200],
            )
            return tag_error(
                ErrorCode.INVALID_REQUEST,
                "❌ Opencode отклонил этот запрос для текущего ключа или модели.",
            )
        return super()._build_http_error_tag(status, response_text, model_name)

    async def _execute_request(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        timeout: float,
        thinking_level: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AIResponse:
        if not self._uses_messages_transport(model_name):
            # Inject tools into the parent call via _extra_payload_params override.
            # We store them on self temporarily so super() picks them up.
            self._pending_tools = tools if (tools and self._supports_tools(model_name)) else None
            self._pending_tool_choice = tool_choice if self._pending_tools else None
            try:
                return await super()._execute_request(
                    history=history,
                    model_name=model_name,
                    system_instruction=system_instruction,
                    user_id=user_id,
                    chat_id=chat_id,
                    timeout=timeout,
                    thinking_level=thinking_level,
                )
            finally:
                self._pending_tools = None
                self._pending_tool_choice = None

        start_time = None
        try:
            await openrouter_provider.metrics_collector.record_api_call("opencode", model_name)
            start_time = time.time()

            payload = await self._build_messages_payload(history, model_name, system_instruction)
            if not payload["messages"]:
                msg = "Failed to create valid messages for Opencode Messages API"
                logging.error(msg)
                await openrouter_provider.metrics_collector.record_error("opencode_content_creation", msg)
                return AIResponse(
                    text=f"❌ {msg}",
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            url = self._get_url_for_model(model_name)
            headers = self._get_headers_for_model(model_name)

            try:
                client = openrouter_provider._openrouter_http_client
                if client is None:
                    raise RuntimeError("OpenRouter HTTP client not initialized")
                response = await asyncio.wait_for(
                    client.post(url, json=payload, headers=headers),
                    timeout=90.0,
                )
                response.raise_for_status()
                response_data = response.json()
            except httpx.HTTPStatusError as e:
                return await self._handle_http_error(e, model_name, start_time, user_id, chat_id)
            except TimeoutError:
                msg = f"Opencode Messages API request timed out for model {model_name}"
                logging.error(msg)
                await openrouter_provider.metrics_collector.record_error("opencode_timeout", msg)
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
            except httpx.HTTPError as e:
                msg = f"Opencode Messages API error: {e!r}"
                logging.error(msg)
                await openrouter_provider.metrics_collector.record_error("opencode_api", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {msg}"),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            response_text = self._extract_messages_text(response_data)
            if not response_text:
                msg = "Opencode Messages API returned empty response"
                logging.error("%s body=%s", msg, response_data)
                await openrouter_provider.metrics_collector.record_error("opencode_empty_response", msg)
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

            usage = response_data.get("usage") or {}
            token_count = self._messages_token_count(usage)
            cached_tokens = usage.get("cache_read_input_tokens", 0)
            if cached_tokens:
                logging.debug(
                    "Opencode prompt cache hit: model=%s cached=%d total=%d",
                    model_name,
                    cached_tokens,
                    token_count,
                )
            if start_time is not None:
                openrouter_provider.api_logger.log_response(
                    "opencode",
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
        except Exception as e:
            logging.error("Opencode Messages API generic error: %s", e, exc_info=True)
            await openrouter_provider.metrics_collector.record_error("opencode_api", str(e))
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            return AIResponse(
                text=tag_error(ErrorCode.GENERIC, f"❌ Произошла непредвиденная ошибка API: {e}"),
                token_count=0,
                success=False,
                error_message=str(e),
                provider=self.provider_name,
                model=model_name,
            )

    async def _build_messages_payload(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None,
    ) -> dict[str, Any]:
        api_model = self._strip_model_prefix(model_name)
        messages: list[dict[str, Any]] = []
        system_segments: list[str] = []

        system_text = str(system_instruction).strip() if system_instruction else ""
        if system_text:
            system_segments.append(system_text)

        for item in history:
            role = str(item.get("role", "user") or "user")
            if role == "model":
                role = "assistant"
            parts = item.get("parts", [])
            if not isinstance(parts, list):
                parts = [parts] if parts is not None else []

            content = await self._build_anthropic_content(parts)
            if not content:
                continue

            if role == "system":
                if isinstance(content, str):
                    system_segments.append(content)
                else:
                    system_segments.append(self._flatten_text_blocks(content))
                continue

            if role not in {"user", "assistant"}:
                role = "user"
            messages.append({"role": role, "content": content})

        payload: dict[str, Any] = {
            "model": api_model,
            "messages": messages,
            "max_tokens": self._MESSAGES_MAX_TOKENS,
        }
        if system_segments:
            payload["system"] = "\n\n".join(segment for segment in system_segments if segment.strip())
        return payload

    async def _build_anthropic_content(self, parts: list[Any]) -> str | list[dict[str, Any]] | None:
        content_blocks: list[dict[str, Any]] = []

        for part in parts:
            image_block = await self._build_anthropic_image_block(part)
            if image_block is not None:
                content_blocks.append(image_block)
                continue

            text = str(part)
            if text.strip():
                content_blocks.append({"type": "text", "text": text})

        if not content_blocks:
            return None
        if all(block.get("type") == "text" for block in content_blocks):
            return "\n".join(
                str(block.get("text", "")) for block in content_blocks if str(block.get("text", "")).strip()
            )
        return content_blocks

    async def _build_anthropic_image_block(self, part: Any) -> dict[str, Any] | None:
        img_bytes: bytes | None = None
        if isinstance(part, TaggedImage):
            if part.pre_compressed:
                img_bytes = part.data
            else:
                img_bytes = await save_image_as_bytes(
                    part.data,
                    cache_key=part.cache_key,
                    task_type=part.task_type,
                )
        elif isinstance(part, (bytes, bytearray, Image.Image)):
            img_bytes = await save_image_as_bytes(part)

        if not img_bytes:
            return None

        img_b64 = await asyncio.to_thread(lambda b: base64.b64encode(b).decode("utf-8"), img_bytes)
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img_b64,
            },
        }

    def _flatten_text_blocks(self, content_blocks: list[dict[str, Any]]) -> str:
        return "\n".join(
            str(block.get("text", ""))
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip()
        )

    def _extract_messages_text(self, response_data: dict[str, Any]) -> str:
        content = response_data.get("content")
        if not isinstance(content, list):
            return ""
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text", ""))
                if text:
                    text_parts.append(text)
        return "".join(text_parts).strip()

    def _messages_token_count(self, usage: Any) -> int:
        if not isinstance(usage, dict):
            return 0
        total = 0
        for key, value in usage.items():
            if key.endswith("_tokens") and isinstance(value, int):
                total += value
        return total

    def _log_failure(
        self,
        start_time: Any,
        model: str,
        msg: str,
        user_id: Any,
        chat_id: Any,
    ) -> None:
        """Log provider-specific failure for Opencode Go requests."""
        logging.error(
            "Opencode Go request failed: model=%s user=%s chat=%s error=%s",
            model,
            user_id,
            chat_id,
            msg,
        )

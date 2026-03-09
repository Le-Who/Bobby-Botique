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
from app.request_context import get_request_id
from app.utils.api_logger import api_logger
from app.utils.image_utils import save_image_as_bytes
from app.utils.network import NetworkErrorHandler

# Module-level httpx client for OpenRouter
_openrouter_http_client: httpx.AsyncClient | None = NetworkErrorHandler.create_robust_http_client()


async def close_http_clients() -> None:
    """Close module-level HTTP clients on shutdown (prevents resource warnings)."""
    global _openrouter_http_client
    if _openrouter_http_client is not None:
        await _openrouter_http_client.aclose()
        _openrouter_http_client = None
        logging.info("OpenRouter HTTP client closed")


class OpenRouterProvider(BaseAIProvider):
    """OpenRouter AI provider — self-contained execution logic."""

    provider_name = "openrouter"

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
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/gemaibotv2",
                "X-Title": "GeminiBot v2",
            }
            request_id = get_request_id()
            if request_id:
                headers["X-Request-ID"] = request_id

            payload = {"model": model_name, "messages": messages}
            logging.debug("OpenRouter: sending %d messages to %s", len(messages), model_name)

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
                    text=tag_error(ErrorCode.TIMEOUT, "⏰ Превышено время ожидания ответа от API. Попробуйте позже."),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )
            except (APIError, httpx.HTTPError) as e:
                msg = f"OpenRouter API error: {e}"
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
                    text=tag_error(ErrorCode.INVALID_RESPONSE, "❌ API вернул некорректный ответ. Попробуйте еще раз."),
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
                    text=tag_error(ErrorCode.EMPTY_RESPONSE, "❌ API вернул пустой ответ. Попробуйте еще раз."),
                    token_count=0,
                    success=False,
                    error_message=msg,
                    provider=self.provider_name,
                    model=model_name,
                )

            token_count = response_data.get("usage", {}).get("total_tokens", 0)

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

    async def stream_response(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None = None,
        thinking_level: str | None = None,
        timeout: float = 120.0,
    ):
        """
        Stream response from OpenRouter API using Server-Sent Events (SSE).
        Yields text chunks.
        """
        import json

        messages = await self._build_messages(history, system_instruction)
        if not messages:
            yield tag_error(ErrorCode.GENERIC, "❌ Failed to create valid messages for OpenRouter")
            return

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/gemaibotv2",
            "X-Title": "GeminiBot v2",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id

        payload = {"model": model_name, "messages": messages, "stream": True}
        
        if _openrouter_http_client is None:
            yield tag_error(ErrorCode.GENERIC, "❌ OpenRouter HTTP client not initialized")
            return

        try:
            async with _openrouter_http_client.stream(
                "POST", url, json=payload, headers=headers, timeout=timeout
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            chunk = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if chunk:
                                yield chunk
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                yield tag_error(ErrorCode.RATE_LIMIT, "⏱️ Превышен лимит запросов. Подождите немного.")
            elif status == 401:
                yield tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ. Проверьте настройки.")
            elif status == 402:
                yield tag_error(ErrorCode.QUOTA_EXCEEDED, "💳 Недостаточно средств на счету OpenRouter.")
            else:
                yield tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {status}")
        except Exception as e:
            logging.error("OpenRouter streaming error: %s", e)
            yield tag_error(ErrorCode.GENERIC, f"❌ Произошла непредвиденная ошибка API: {e}")


    # ── OpenRouter helpers ───────────────────────────────────────────────

    async def _build_messages(self, history: list, system_instruction: str | None) -> list:
        """Convert Gemini-format history → OpenAI-format messages."""
        messages = []
        if system_instruction:
            content = str(system_instruction).strip()
            if content:
                messages.append({"role": "system", "content": content})

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
                if isinstance(part, (bytes, bytearray, Image.Image)):
                    img_bytes = await save_image_as_bytes(part)
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
        if status == 429:
            text = tag_error(ErrorCode.RATE_LIMIT, "⏱️ Превышен лимит запросов. Подождите немного.")
        elif status == 401:
            text = tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ. Проверьте настройки.")
        elif status == 402:
            text = tag_error(ErrorCode.QUOTA_EXCEEDED, "💳 Недостаточно средств на счету OpenRouter.")
        elif status == 503:
            text = tag_error(ErrorCode.OVERLOADED, "🔄 Сервер OpenRouter перегружен. Попробуйте позже.")
        else:
            text = tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {status}")

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
            if isinstance(part, Image.Image):
                return True
            if isinstance(part, (bytes, bytearray)):
                return True
    return False

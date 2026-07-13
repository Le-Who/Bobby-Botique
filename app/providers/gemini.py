"""Google Gemini AI provider — self-contained execution logic."""

import asyncio
import logging
import time
from typing import Any

import httpx
from cachetools import LRUCache
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image

from app.config import settings
from app.errors import ErrorCode, tag_error
from app.metrics import metrics_collector
from app.providers.base import AIResponse, BaseAIProvider, _build_thinking_config
from app.utils.api_logger import api_logger
from app.utils.image_utils import TaggedImage, save_image_as_bytes
from app.utils.text_utils import get_part_length

# Global cache for genai.Client instances to reuse connection pools (TLS/TCP)
# Key: API Key (string), Value: genai.Client
_gemini_clients_cache: LRUCache = LRUCache(maxsize=50)


def get_cached_genai_client(api_key: str) -> genai.Client:
    """Return a cached genai.Client for the given API key, creating one if needed."""
    if api_key not in _gemini_clients_cache:
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        http_opts: dict[str, Any] = {"timeout": 90_000}
        client_kwargs["http_options"] = types.HttpOptions(**http_opts)  # type: ignore[arg-type]
        _gemini_clients_cache[api_key] = genai.Client(**client_kwargs)  # type: ignore[arg-type]
    return _gemini_clients_cache[api_key]


class GeminiProvider(BaseAIProvider):
    """Google Gemini AI provider — self-contained execution logic."""

    provider_name = "gemini"

    def __init__(self, api_key: str):
        # We call super() first to validate the key type
        super().__init__(api_key)
        # Reuse cached client via shared factory
        self._client = get_cached_genai_client(api_key)
        self._client_api_key: str = api_key

    async def _execute_request(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        timeout: float,
        thinking_level: str | None = None,
        enable_web_search: bool = False,
    ) -> AIResponse:
        start_time = None

        try:
            await metrics_collector.record_api_call("gemini", model_name)

            # Compute metrics
            try:
                prompt_length = sum(
                    get_part_length(part) for item in history for part in (item.get("parts", []) or []) if part is not None
                )
                has_images = any(
                    isinstance(part, (bytes, bytearray, Image.Image))
                    for item in history
                    for part in (item.get("parts", []) or [])
                    if part is not None
                )
            except Exception as e:
                logging.warning("Metrics calc error: %s", e)
                prompt_length = 0
                has_images = False

            start_time = api_logger.log_request(
                "gemini",
                model=model_name,
                prompt_length=prompt_length,
                has_images=has_images,
            )

            client = self._client

            # Convert history → types.Content
            contents = await self._build_contents(history)
            if contents is None:
                return self._error_response(
                    "Failed to create valid content for Gemini API",
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            config = types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)  # type: ignore[arg-type]  # Pydantic coerces dicts→SafetySetting
            # Apply Google Search Grounding if requested
            if enable_web_search:
                config.tools = [types.Tool(google_search=types.GoogleSearch())]
            # Apply thinking config if user requested a specific level
            tc = _build_thinking_config(model_name, thinking_level)
            if tc:
                config.thinking_config = tc
            if system_instruction:
                try:
                    config.system_instruction = str(system_instruction)
                except (TypeError, ValueError) as e:
                    logging.warning("Failed to set system_instruction: %s", e)

            # Native async call — properly supports CancelledError
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                ),
                timeout=timeout,
            )

            # Extract token count from response metadata (free, no extra API call).
            # Falls back to 0 if usage_metadata is unavailable.
            try:
                usage = getattr(response, "usage_metadata", None)
                token_count = getattr(usage, "total_token_count", 0) or getattr(usage, "candidates_token_count", 0) or 0
            except Exception as e:
                logging.debug("Token count from usage_metadata failed: %s", e)
                token_count = 0

            # Validate response
            if not response or not hasattr(response, "text"):
                return self._error_response(
                    "Gemini API returned invalid response object",
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            response_text = response.text if response.text else ""
            if not response_text:
                # Inspect WHY the response is empty — safety block, prompt block, etc.
                block_reason = self._diagnose_empty_response(response)
                return self._error_response(
                    block_reason,
                    model_name,
                    start_time,
                    user_id,
                    chat_id,
                )

            # Log success
            if start_time is not None:
                api_logger.log_response(
                    "gemini",
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

        except TimeoutError:
            msg = f"Gemini API request timed out for model {model_name}"
            logging.error(msg)
            await metrics_collector.record_error("gemini_timeout", msg)
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

        except APIError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini API Error: %s", e)
            err_lower = str(e).lower()

            if "quota" in err_lower:
                await metrics_collector.record_error("gemini_quota", str(e))
                text = tag_error(
                    ErrorCode.QUOTA_EXCEEDED,
                    "🚫 Достигнут лимит запросов к API (Quota Exceeded).",
                )
            elif "503" in str(e) or "unavailable" in err_lower or "overloaded" in err_lower:
                await metrics_collector.record_error("gemini_overloaded", str(e))
                raise  # Trigger retry in BaseAIProvider
            elif "api key" in err_lower or "api_key_invalid" in err_lower:
                await metrics_collector.record_error("gemini_invalid_key", str(e))
                text = tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ.")
            elif "invalid" in err_lower or "malformed" in err_lower:
                await metrics_collector.record_error("gemini_invalid_request", str(e))
                text = tag_error(
                    ErrorCode.INVALID_REQUEST,
                    "❌ Некорректный запрос к API. Проверьте параметры.",
                )
            elif "rate limit" in err_lower:
                await metrics_collector.record_error("gemini_rate_limit", str(e))
                text = tag_error(
                    ErrorCode.RATE_LIMIT,
                    "⏱️ Превышен лимит запросов в секунду. Подождите немного.",
                )
            else:
                await metrics_collector.record_error("gemini_api_call", str(e))
                text = tag_error(ErrorCode.GENERIC, f"Произошла ошибка вызова API: {e}")

            return AIResponse(
                text=text,
                token_count=0,
                success=False,
                error_message=str(e),
                provider=self.provider_name,
                model=model_name,
            )

        except httpx.HTTPError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini HTTP error: %s", e, exc_info=True)
            await metrics_collector.record_error("gemini_http", str(e))
            return AIResponse(
                text=tag_error(ErrorCode.NETWORK, f"Произошла непредвиденная ошибка HTTP: {e}"),
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
        enable_web_search: bool = False,
    ):
        """
        Stream response from Gemini API.
        Yields text chunks.
        """
        if self._client is None or self._client_api_key != self.api_key:
            client_kwargs: dict[str, Any] = {"api_key": self.api_key}
            http_opts: dict[str, Any] = {"timeout": 90_000}
            client_kwargs["http_options"] = types.HttpOptions(**http_opts)
            self._client = genai.Client(**client_kwargs)
            self._client_api_key = self.api_key
        client = self._client

        contents = await self._build_contents(history)
        if contents is None:
            yield tag_error(ErrorCode.GENERIC, "❌ Failed to create valid content for Gemini")
            return

        config = types.GenerateContentConfig(safety_settings=settings.SAFETY_SETTINGS)  # type: ignore[arg-type]
        # Apply Google Search Grounding if requested
        if enable_web_search:
            config.tools = [types.Tool(google_search=types.GoogleSearch())]
        tc = _build_thinking_config(model_name, thinking_level)
        if tc:
            config.thinking_config = tc
        if system_instruction:
            config.system_instruction = str(system_instruction)

        _content_yielded = False
        try:
            # wait_for to prevent hanging during connect
            coro = client.aio.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=config,
            )
            response_stream = await asyncio.wait_for(coro, timeout=timeout)
            async for chunk in response_stream:
                try:
                    candidates = getattr(chunk, "candidates", None)
                    if candidates:
                        fr = getattr(candidates[0], "finish_reason", None)
                        if fr and str(fr) != "FINISH_REASON_UNSPECIFIED":
                            from app.streaming import set_last_finish_reason

                            set_last_finish_reason(str(fr))
                    # Extract usage_metadata from the last chunk (Gemini provides it on final chunk)
                    usage = getattr(chunk, "usage_metadata", None)
                    if usage:
                        total = getattr(usage, "total_token_count", 0) or 0
                        if total > 0:
                            from app.streaming import set_last_token_count

                            set_last_token_count(total)
                except Exception as e:
                    logging.debug("Error extracting stream finish_reason: %s", e)

                if chunk.text:
                    _content_yielded = True
                    yield chunk.text
        except TimeoutError:
            logging.error("Gemini API stream timed out for model %s", model_name)
            if not _content_yielded:
                raise  # Let router rotate keys
            # Mid-stream timeout: raise so streaming.py can finalize cleanly
            # instead of dumping raw error text into the user's message.
            raise
        except APIError as e:
            err_lower = str(e).lower()
            is_retryable = (
                "503" in str(e) or "unavailable" in err_lower or "overloaded" in err_lower or "rate limit" in err_lower
            )

            if not _content_yielded:
                if is_retryable:
                    # Re-raise so the router can rotate to a different API key.
                    # Google 503 "high demand" errors are often per-project or
                    # per-key — a different key may succeed immediately.
                    logging.warning(
                        "Gemini API retryable stream error (503/UNAVAILABLE), re-raising for key rotation: %s", e
                    )
                    raise

                # Pre-stream non-retryable errors: yield tagged error for the UI
                logging.error("Gemini API stream error (pre-content): %s", e)
                if "quota" in err_lower:
                    yield tag_error(ErrorCode.QUOTA_EXCEEDED, "🚫 Достигнут лимит запросов к API.")
                elif "api key" in err_lower or "api_key_invalid" in err_lower:
                    yield tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ.")
                elif "invalid" in err_lower or "malformed" in err_lower:
                    yield tag_error(ErrorCode.INVALID_REQUEST, "❌ Некорректный запрос к API.")
                elif "rate limit" in err_lower:
                    yield tag_error(ErrorCode.RATE_LIMIT, "⏱️ Превышен лимит запросов.")
                else:
                    yield tag_error(ErrorCode.GENERIC, f"❌ Произошла ошибка API: {e}")
            else:
                # Mid-stream error: raise so streaming.py can finalize the
                # partial text cleanly instead of injecting raw JSON into chat.
                logging.error("Gemini API mid-stream error, escalating: %s", e)
                raise
        except Exception as e:
            if not _content_yielded:
                raise  # Let router rotate keys
            # Mid-stream error: raise for clean handling by streaming.py
            logging.error("Gemini mid-stream error, escalating: %s", e)
            raise

    # ── Gemini helpers ───────────────────────────────────────────────────

    async def _build_contents(self, history: list) -> list | None:
        """Convert history dicts → list[types.Content]. Returns None on total failure."""
        contents = []
        try:
            for item in history:
                if not isinstance(item, dict):
                    logging.warning("Skipping invalid history item (not dict): %s", type(item))
                    continue
                role = item.get("role", "user")
                parts = item.get("parts", [])
                if not isinstance(parts, list):
                    parts = [parts] if parts is not None else []

                processed = []
                for part in parts:
                    if isinstance(part, TaggedImage):
                        if part.pre_compressed:
                            img_bytes = part.data
                        else:
                            img_bytes = await save_image_as_bytes(  # type: ignore[assignment]  # bytes | None from save_image_as_bytes
                                part.data, cache_key=part.cache_key, task_type=part.task_type
                            )
                        if img_bytes:
                            try:
                                processed.append(
                                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=img_bytes))
                                )
                            except (TypeError, ValueError) as e:
                                logging.warning("Failed to create image part: %s", e)
                        else:
                            logging.warning("Skipping TaggedImage part due to processing error")
                    elif isinstance(part, (bytes, bytearray, Image.Image)):
                        img_bytes_raw: bytes | None = await save_image_as_bytes(part)
                        if img_bytes_raw:
                            try:
                                processed.append(
                                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=img_bytes_raw))
                                )
                            except (TypeError, ValueError) as e:
                                logging.warning("Failed to create image part: %s", e)
                        else:
                            logging.warning("Skipping image part due to processing error")
                    else:
                        try:
                            processed.append(types.Part.from_text(text=str(part)))
                        except (TypeError, ValueError) as e:
                            logging.warning("Failed to process text part: %s", e)

                if processed:
                    try:
                        contents.append(types.Content(role=role, parts=processed))
                    except (TypeError, ValueError) as e:
                        logging.warning("Failed to create Content object: %s", e)
        except Exception as e:
            logging.error("Error processing history: %s", e, exc_info=True)
            return None

        return contents if contents else None

    def _diagnose_empty_response(self, response) -> str:
        """Inspect Gemini response to determine why text is empty."""
        # 1. Check prompt-level block (input was blocked)
        try:
            pf = getattr(response, "prompt_feedback", None)
            if pf:
                block_reason = getattr(pf, "block_reason", None)
                if block_reason:
                    logging.warning(
                        "Gemini prompt blocked: reason=%s, feedback=%s",
                        block_reason,
                        pf,
                    )
                    return "Запрос заблокирован фильтром безопасности Google. Попробуйте переформулировать сообщение."
        except Exception as e:
            logging.debug("prompt_feedback inspection error: %s", e)

        # 2. Check candidate-level finish reason
        try:
            candidates = getattr(response, "candidates", None)
            if candidates:
                candidate = candidates[0]
                finish_reason = getattr(candidate, "finish_reason", None)
                safety_ratings = getattr(candidate, "safety_ratings", None)

                if finish_reason and str(finish_reason).upper() in (
                    "SAFETY",
                    "2",
                    "FINISH_REASON_SAFETY",
                ):
                    ratings_str = ""
                    if safety_ratings:
                        ratings_str = ", ".join(
                            f"{getattr(r, 'category', '?')}={getattr(r, 'probability', '?')}" for r in safety_ratings
                        )
                    logging.warning(
                        "Gemini response safety-blocked: finish_reason=%s, ratings=[%s]",
                        finish_reason,
                        ratings_str,
                    )
                    return "Ответ заблокирован фильтром безопасности Google. Попробуйте переформулировать сообщение."

                if finish_reason and str(finish_reason).upper() in (
                    "MAX_TOKENS",
                    "3",
                    "FINISH_REASON_MAX_TOKENS",
                ):
                    logging.warning("Gemini response truncated: MAX_TOKENS")
                    return "Ответ превысил максимальную длину. Попробуйте более короткий запрос."

                if finish_reason and str(finish_reason).upper() in (
                    "RECITATION",
                    "4",
                    "FINISH_REASON_RECITATION",
                ):
                    logging.warning("Gemini response blocked: RECITATION")
                    return "Ответ заблокирован из-за совпадения с защищённым контентом."

                logging.warning(
                    "Gemini empty response with finish_reason=%s",
                    finish_reason,
                )
            else:
                logging.warning("Gemini response has no candidates")
        except Exception as e:
            logging.debug("candidate inspection error: %s", e)

        return "Gemini API вернул пустой ответ. Попробуйте ещё раз."

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        if start_time is not None:
            api_logger.log_response(
                "gemini",
                start_time,
                model=model,
                response_length=0,
                success=False,
                error_message=msg,
            )

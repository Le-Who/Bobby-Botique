"""
AI Provider abstraction layer for unified API interactions.

This module provides:
- BaseAIProvider abstract class defining common interface
- GeminiProvider and OpenRouterProvider implementations
- ProviderRouter with per-key health scoring and automatic failover
- Unified get_ai_response() function with automatic provider selection
- Common validation, retry logic, and error handling
"""

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image

from app.config import settings
from app.errors import (
    ErrorCode,
    classify_key_error,
    is_error_message,
    is_key_related_error,
    tag_error,
    user_friendly_error,
)
from app.metrics import metrics_collector
from app.request_context import get_request_id
from app.resilience_policy import ResiliencePolicy, run_with_resilience
from app.utils.api_logger import api_logger
from app.utils.image_utils import save_image_as_bytes
from app.utils.network import NetworkErrorHandler


# ── Thinking config helpers ──────────────────────────────────────────

_THINKING_BUDGET_MAP = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}
_THINKING_LEVEL_MAP = {"off": "minimal", "low": "low", "medium": "medium", "high": "high"}


def _is_gemini3_model(model_name: str) -> bool:
    """Detect Gemini 3.x models that require thinkingLevel instead of thinkingBudget."""
    return "gemini-3" in model_name or model_name == "gemini-flash-latest"


def _build_thinking_config(model_name: str, thinking_level: str | None) -> types.ThinkingConfig | None:
    """Build ThinkingConfig for the appropriate model family.

    - Gemini 3.x → thinkingLevel (minimal/low/medium/high)
    - Gemini 2.5  → thinkingBudget (int: 0-24576)
    - Other models (OpenRouter, etc.) → None (not supported)

    Returns None if thinking_level is None or model doesn't support thinking.
    """
    if not thinking_level or thinking_level not in _THINKING_BUDGET_MAP:
        return None
    # Only Gemini models support thinking
    if is_openrouter_model(model_name):
        return None
    if _is_gemini3_model(model_name):
        return types.ThinkingConfig(thinking_level=_THINKING_LEVEL_MAP[thinking_level])
    return types.ThinkingConfig(thinking_budget=_THINKING_BUDGET_MAP[thinking_level])


@dataclass
class AIResponse:
    """Standardized response from any AI provider."""

    text: str
    token_count: int
    success: bool
    error_message: str | None = None
    provider: str = ""
    model: str = ""

    @property
    def is_error(self) -> bool:
        return not self.success or self.error_message is not None


class BaseAIProvider(ABC):
    """
    Abstract base class for AI providers.

    Implements common patterns:
    - Input validation
    - Retry logic with exponential backoff
    - Error categorization
    - Metrics collection
    """

    provider_name: str = "base"

    def __init__(self, api_key: str):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self.api_key = api_key

    async def get_response(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        max_retries: int = 3,
        timeout: float = 120.0,
        thinking_level: str | None = None,
    ) -> AIResponse:
        """
        Get response from AI provider with retry logic.

        Args:
            history: Message history in Gemini format
            model_name: Model identifier
            system_instruction: Optional system prompt
            user_id: User ID for logging
            chat_id: Chat ID for logging
            max_retries: Maximum retry attempts for transient errors
            timeout: Request timeout in seconds

        Returns:
            AIResponse with text and metadata
        """
        # Validate inputs — returns error string on failure, None on success
        validation_error = self._validate_inputs(history, model_name, user_id, chat_id)
        if validation_error:
            logging.error("Input validation failed: %s", validation_error)
            return AIResponse(
                text=f"❌ {validation_error}",
                token_count=0,
                success=False,
                error_message=validation_error,
                provider=self.provider_name,
                model=model_name or "unknown",
            )

        policy = ResiliencePolicy(max_retries=max_retries, timeout_s=timeout)
        last_error = None

        async def _operation() -> AIResponse:
            return await self._execute_request(
                history=history,
                model_name=model_name,
                system_instruction=system_instruction,
                user_id=user_id,
                chat_id=chat_id,
                timeout=timeout,
                thinking_level=thinking_level,
            )

        try:
            response, _ = await run_with_resilience(
                _operation,
                policy,
                circuit_name=f"ai_provider:{self.provider_name}",
                is_retryable=lambda e: self._is_transient_error(str(e)),
            )
            return response
        except (APIError, httpx.HTTPError) as e:
            last_error = e

        error_msg = user_friendly_error(last_error) if last_error else "Unknown error"
        return AIResponse(
            text=error_msg,
            token_count=0,
            success=False,
            error_message=str(last_error),
            provider=self.provider_name,
            model=model_name,
        )

    def _validate_inputs(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        user_id: int | None,
        chat_id: int | None,
    ) -> str | None:
        """Validate common input parameters.

        Returns:
            None if valid, or an error message string if invalid.
        """
        if not isinstance(history, list) or not history:
            return "history must be a non-empty list"

        if not isinstance(model_name, str) or not model_name.strip():
            return "model_name must be a non-empty string"

        if user_id is not None and not isinstance(user_id, int):
            return "user_id must be an integer"

        if chat_id is not None and not isinstance(chat_id, int):
            return "chat_id must be an integer"

        return None

    def _is_transient_error(self, error_text: str) -> bool:
        """Check if error is transient and can be retried."""
        transient_patterns = [
            "503",
            "unavailable",
            "overloaded",
            "rate limit",
            "timeout",
            "connection",
            "temporarily",
        ]
        error_lower = error_text.lower()
        return any(pattern in error_lower for pattern in transient_patterns)

    @abstractmethod
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
        """
        Execute the actual API request. Must be implemented by subclasses.
        """
        pass




def is_openrouter_model(model_name: str) -> bool:
    """Check if model name indicates an OpenRouter model."""
    return "/" in model_name


def get_provider_for_model(model_name: str, api_key: str) -> BaseAIProvider:
    """
    Factory function to get appropriate provider for a model.

    Args:
        model_name: Model identifier
        api_key: API key for the provider

    Returns:
        Appropriate AIProvider instance
    """
    if is_openrouter_model(model_name):
        return OpenRouterProvider(api_key)
    else:
        return GeminiProvider(api_key)


class GeminiProvider(BaseAIProvider):
    """Google Gemini AI provider — self-contained execution logic."""

    provider_name = "gemini"
    _client: Any = None  # Lazily-cached genai.Client
    _client_api_key: str | None = None  # Track which key the cached client uses

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
            await metrics_collector.record_api_call("gemini", model_name)

            # Compute metrics
            try:
                prompt_length = sum(
                    len(str(part))
                    for item in history
                    for part in (item.get("parts", []) or [])
                    if part is not None
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

            start_time = api_logger.log_gemini_request(
                model=model_name,
                prompt_length=prompt_length,
                has_images=has_images,
                user_id=user_id,
                chat_id=chat_id,
            )

            # Reuse client across requests (connection pooling, TLS caching).
            # Rebuild only when api_key changes or on first call.
            request_id = get_request_id()
            if self._client is None or self._client_api_key != self.api_key:
                client_kwargs = {"api_key": self.api_key}
                http_opts = {"timeout": 90_000}  # 90s SDK deadline
                client_kwargs["http_options"] = types.HttpOptions(**http_opts)
                self._client = genai.Client(**client_kwargs)
                self._client_api_key = self.api_key
            client = self._client

            # Convert history → types.Content
            contents = await self._build_contents(history)
            if contents is None:
                return self._error_response(
                    "Failed to create valid content for Gemini API",
                    model_name, start_time, user_id, chat_id,
                )

            config = types.GenerateContentConfig(
                safety_settings=settings.SAFETY_SETTINGS
            )
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
                    model=model_name, contents=contents, config=config,
                ),
                timeout=100.0,
            )

            # Extract token count from response metadata (free, no extra API call).
            # Falls back to 0 if usage_metadata is unavailable.
            try:
                usage = getattr(response, "usage_metadata", None)
                token_count = (
                    getattr(usage, "total_token_count", 0)
                    or getattr(usage, "candidates_token_count", 0)
                    or 0
                )
            except Exception as e:
                logging.debug("Token count from usage_metadata failed: %s", e)
                token_count = 0

            # Validate response
            if not response or not hasattr(response, "text"):
                return self._error_response(
                    "Gemini API returned invalid response object",
                    model_name, start_time, user_id, chat_id,
                )

            response_text = response.text if response.text else ""
            if not response_text:
                # Inspect WHY the response is empty — safety block, prompt block, etc.
                block_reason = self._diagnose_empty_response(response)
                return self._error_response(
                    block_reason,
                    model_name, start_time, user_id, chat_id,
                )

            # Log success
            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time, model=model_name,
                    response_length=len(response_text),
                    token_count=token_count, success=True,
                    user_id=user_id, chat_id=chat_id,
                )

            return AIResponse(
                text=response_text, token_count=token_count,
                success=True, provider=self.provider_name, model=model_name,
            )

        except TimeoutError:
            msg = f"Gemini API request timed out for model {model_name}"
            logging.error(msg)
            await metrics_collector.record_error("gemini_timeout", msg)
            self._log_failure(start_time, model_name, msg, user_id, chat_id)
            return AIResponse(
                text=tag_error(ErrorCode.TIMEOUT, "⏰ Превышено время ожидания ответа от API. Попробуйте позже."),
                token_count=0, success=False, error_message=msg,
                provider=self.provider_name, model=model_name,
            )

        except APIError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini API Error: %s", e)
            err_lower = str(e).lower()

            if "quota" in err_lower:
                await metrics_collector.record_error("gemini_quota", str(e))
                text = tag_error(ErrorCode.QUOTA_EXCEEDED, "🚫 Достигнут лимит запросов к API (Quota Exceeded).")
            elif "503" in str(e) or "unavailable" in err_lower or "overloaded" in err_lower:
                await metrics_collector.record_error("gemini_overloaded", str(e))
                raise  # Trigger retry in BaseAIProvider
            elif "api key" in err_lower or "api_key_invalid" in err_lower:
                await metrics_collector.record_error("gemini_invalid_key", str(e))
                text = tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ.")
            elif "invalid" in err_lower or "malformed" in err_lower:
                await metrics_collector.record_error("gemini_invalid_request", str(e))
                text = tag_error(ErrorCode.INVALID_REQUEST, "❌ Некорректный запрос к API. Проверьте параметры.")
            elif "rate limit" in err_lower:
                await metrics_collector.record_error("gemini_rate_limit", str(e))
                text = tag_error(ErrorCode.RATE_LIMIT, "⏱️ Превышен лимит запросов в секунду. Подождите немного.")
            else:
                await metrics_collector.record_error("gemini_api_call", str(e))
                text = tag_error(ErrorCode.GENERIC, f"Произошла ошибка вызова API: {e}")

            return AIResponse(
                text=text, token_count=0, success=False,
                error_message=str(e), provider=self.provider_name, model=model_name,
            )

        except httpx.HTTPError as e:
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            logging.error("Gemini HTTP error: %s", e, exc_info=True)
            await metrics_collector.record_error("gemini_http", str(e))
            return AIResponse(
                text=tag_error(ErrorCode.NETWORK, f"Произошла непредвиденная ошибка HTTP: {e}"),
                token_count=0, success=False, error_message=str(e),
                provider=self.provider_name, model=model_name,
            )

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
                    if isinstance(part, (bytes, bytearray, Image.Image)):
                        img_bytes = await save_image_as_bytes(part)
                        if img_bytes:
                            try:
                                processed.append(types.Part(
                                    inline_data=types.Blob(
                                        mime_type="image/jpeg", data=img_bytes
                                    )
                                ))
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
            try:
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text("Error processing request")],
                ))
            except Exception:
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
                        block_reason, pf,
                    )
                    return (
                        "Запрос заблокирован фильтром безопасности Google. "
                        "Попробуйте переформулировать сообщение."
                    )
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
                    "SAFETY", "2", "FINISH_REASON_SAFETY",
                ):
                    ratings_str = ""
                    if safety_ratings:
                        ratings_str = ", ".join(
                            f"{getattr(r, 'category', '?')}={getattr(r, 'probability', '?')}"
                            for r in safety_ratings
                        )
                    logging.warning(
                        "Gemini response safety-blocked: finish_reason=%s, ratings=[%s]",
                        finish_reason, ratings_str,
                    )
                    return (
                        "Ответ заблокирован фильтром безопасности Google. "
                        "Попробуйте переформулировать сообщение."
                    )

                if finish_reason and str(finish_reason).upper() in (
                    "MAX_TOKENS", "3", "FINISH_REASON_MAX_TOKENS",
                ):
                    logging.warning("Gemini response truncated: MAX_TOKENS")
                    return "Ответ превысил максимальную длину. Попробуйте более короткий запрос."

                if finish_reason and str(finish_reason).upper() in (
                    "RECITATION", "4", "FINISH_REASON_RECITATION",
                ):
                    logging.warning("Gemini response blocked: RECITATION")
                    return "Ответ заблокирован из-за совпадения с защищённым контентом."

                logging.warning(
                    "Gemini empty response with finish_reason=%s", finish_reason,
                )
            else:
                logging.warning("Gemini response has no candidates")
        except Exception as e:
            logging.debug("candidate inspection error: %s", e)

        return "Gemini API вернул пустой ответ. Попробуйте ещё раз."

    def _error_response(
        self, msg: str, model: str, start_time, user_id, chat_id,
    ) -> AIResponse:
        logging.error(msg)
        self._log_failure(start_time, model, msg, user_id, chat_id)
        return AIResponse(
            text=f"❌ {msg}", token_count=0, success=False,
            error_message=msg, provider=self.provider_name, model=model,
        )

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        if start_time is not None:
            api_logger.log_gemini_response(
                start_time=start_time, model=model, response_length=0,
                success=False, error_message=msg,
                user_id=user_id, chat_id=chat_id,
            )



# Module-level httpx client for OpenRouter
_openrouter_http_client = NetworkErrorHandler.create_robust_http_client()


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
                    text=f"❌ {msg}", token_count=0, success=False,
                    error_message=msg, provider=self.provider_name, model=model_name,
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
                    token_count=0, success=False, error_message=msg,
                    provider=self.provider_name, model=model_name,
                )
            except (APIError, httpx.HTTPError) as e:
                msg = f"OpenRouter API error: {e}"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_api", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(ErrorCode.GENERIC, f"❌ Ошибка API: {msg}"), token_count=0, success=False,
                    error_message=msg, provider=self.provider_name, model=model_name,
                )

            # Validate response structure
            if not response_data or "choices" not in response_data or not response_data["choices"]:
                msg = "OpenRouter API returned invalid response"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_invalid_response", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(ErrorCode.INVALID_RESPONSE, "❌ API вернул некорректный ответ. Попробуйте еще раз."),
                    token_count=0, success=False, error_message=msg,
                    provider=self.provider_name, model=model_name,
                )

            response_text = response_data["choices"][0].get("message", {}).get("content", "")
            if not response_text:
                msg = "OpenRouter API returned empty response"
                logging.error(msg)
                await metrics_collector.record_error("openrouter_empty_response", msg)
                self._log_failure(start_time, model_name, msg, user_id, chat_id)
                return AIResponse(
                    text=tag_error(ErrorCode.EMPTY_RESPONSE, "❌ API вернул пустой ответ. Попробуйте еще раз."),
                    token_count=0, success=False, error_message=msg,
                    provider=self.provider_name, model=model_name,
                )

            token_count = response_data.get("usage", {}).get("total_tokens", 0)

            # Log success
            if start_time is not None:
                api_logger.log_gemini_response(
                    start_time=start_time, model=model_name,
                    response_length=len(response_text),
                    token_count=token_count, success=True,
                    user_id=user_id, chat_id=chat_id,
                )

            return AIResponse(
                text=response_text, token_count=token_count,
                success=True, provider=self.provider_name, model=model_name,
            )

        except (APIError, httpx.HTTPError) as e:
            logging.error("OpenRouter API generic error: %s", e, exc_info=True)
            await metrics_collector.record_error("openrouter_api", str(e))
            self._log_failure(start_time, model_name, str(e), user_id, chat_id)
            return AIResponse(
                text=tag_error(ErrorCode.GENERIC, f"❌ Произошла непредвиденная ошибка API: {e}"),
                token_count=0, success=False, error_message=str(e),
                provider=self.provider_name, model=model_name,
            )

    # ── OpenRouter helpers ───────────────────────────────────────────────

    async def _build_messages(
        self, history: list, system_instruction: str | None
    ) -> list:
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
                        img_b64 = await asyncio.to_thread(
                            lambda b=img_bytes: base64.b64encode(b).decode("utf-8")
                        )
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                        })
                else:
                    text = str(part)
                    if text.strip():
                        content_parts.append({"type": "text", "text": text})

            if content_parts:
                if len(content_parts) == 1 and content_parts[0].get("type") == "text":
                    messages.append({"role": role, "content": content_parts[0]["text"]})
                else:
                    messages.append({"role": role, "content": content_parts})

        return messages

    async def _handle_http_error(
        self, e: httpx.HTTPStatusError, model: str, start_time, user_id, chat_id,
    ) -> AIResponse:
        msg = f"OpenRouter API HTTP error: {e.response.status_code} - {e.response.text}"
        logging.error(msg)
        await metrics_collector.record_error("openrouter_http", msg)
        self._log_failure(start_time, model, msg, user_id, chat_id)

        status = e.response.status_code
        if status == 429:
            text = "⏱️ Превышен лимит запросов. Подождите немного."
        elif status == 401:
            text = "🔑 Неверный API ключ. Проверьте настройки."
        elif status == 402:
            text = "💳 Недостаточно средств на счету OpenRouter."
        elif status == 503:
            text = "🔄 Сервер OpenRouter перегружен. Попробуйте позже."
        else:
            text = f"❌ Ошибка API: {status}"

        return AIResponse(
            text=text, token_count=0, success=False,
            error_message=msg, provider=self.provider_name, model=model,
        )

    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        if start_time is not None:
            api_logger.log_openrouter_response(
                start_time=start_time, model=model, response_length=0,
                success=False, error_message=msg,
                user_id=user_id, chat_id=chat_id,
            )


# ── ProviderRouter ───────────────────────────────────────────────────────────────────


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


class ProviderRouter:
    """
    Routes AI requests to the right provider with key rotation and health scoring.

    Uses DB-backed KeyStatusManager for persistent per-model key health tracking.
    Keys are suspended with error-category-aware cooldowns and automatically
    recover after their cooldown expires (two-tier selection in SQL).
    """

    def __init__(self, rate_limit_per_minute: int = 20) -> None:
        # Use the consolidated RateLimiter from security.py (includes periodic cleanup)
        from app.security import RateLimiter
        self._rate_limiter = RateLimiter(
            max_requests=rate_limit_per_minute, window_seconds=60
        )

    async def get_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
    ) -> tuple[str, int | None]:
        """
        Get AI response with automatic key rotation and health-aware selection.

        Delegates to AgentRequestUseCase for key resolution, which uses
        two-tier SQL (active first, then cooldown-expired) to pick keys.
        On failure, classifies the error and suspends the key with appropriate
        cooldown. On success, promotes the key back to active.
        """
        from app.agent_use_cases import AgentRequestUseCase
        from app.repos.keys import get_key_status_manager

        # Per-user rate limiting (async — RateLimiter from security.py)
        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            return (
                "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
                None,
            )

        # Auto-detect multimodal content → force Gemini
        if use_openrouter is None and _has_multimodal_content(history):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True  # Track if ALL failures are permanent (model-level)

        for attempt in range(max_key_retries):
            key_data, model_used, resolution = await use_case.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                if resolution == "all_exhausted":
                    is_or = (
                        use_openrouter
                        if use_openrouter is not None
                        else ("/" in preferred_model)
                    )
                    provider_name = "OpenRouter" if is_or else "Gemini"
                    return (
                        tag_error(ErrorCode.KEYS_EXHAUSTED, f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже."),
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        tag_error(ErrorCode.NO_KEYS, "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки."),
                        None,
                    )
                if resolution == "decryption_failed":
                    return (
                        tag_error(ErrorCode.DECRYPTION_FAILED, "🔐 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET)."),
                        None,
                    )
                return (
                    tag_error(ErrorCode.KEYS_EXHAUSTED, "🚫 Не удалось получить доступный ключ API. Попробуйте позже."),
                    None,
                )

            # Execute the request
            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                thinking_level=thinking_level,
            )

            # Track health based on response
            if (
                response_text
                and is_error_message(response_text)
                and is_key_related_error(response_text)
            ):
                failed_keys.add(key_data["key_hash"])
                error_category = classify_key_error(response_text)

                if error_category != "permanent":
                    all_permanent = False

                if error_category != "transient":
                    try:
                        await status_mgr.suspend_key(
                            key_data["key_hash"], model_used,
                            error_category, response_text[:200],
                        )
                    except Exception as e:
                        logging.warning(
                            "Non-critical: failed to suspend key: %s", e,
                        )

                logging.warning(
                    "Key %s… failed (category=%s, attempt %d/%d). "
                    "Error: %s",
                    key_data["key_hash"][:8], error_category,
                    attempt + 1, max_key_retries,
                    response_text[:100],
                )
                continue

            # Success — update health and increment usage
            if response_text and not is_error_message(response_text):
                try:
                    await status_mgr.record_success(
                        key_data["key_hash"], model_used,
                    )
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)

                try:
                    await use_case.increment_key_usage(
                        key_data["key_hash"], model_used, use_openrouter
                    )
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)

            return response_text, token_count

        # ── Model-level fallback ─────────────────────────────────────────
        # All keys failed for the preferred model. If every failure was
        # "permanent" (API_KEY_INVALID — Google rejects the key for this
        # specific model), try alternative models before giving up.
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model, history, system_instruction,
                user_id, chat_id, use_openrouter,
                use_case, status_mgr,
            )
            if fallback_result is not None:
                return fallback_result

        is_or = (
            use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        )
        provider_name = "OpenRouter" if is_or else "Gemini"
        return (
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            None,
        )

    async def _try_model_fallback(
        self,
        failed_model: str,
        history: list,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        use_openrouter: bool | None,
        use_case,
        status_mgr,
    ) -> tuple[str, int | None] | None:
        """Try fallback models when all keys fail with permanent errors for one model.

        Returns (response_text, token_count) on success, or None if no fallback works.
        """
        is_or = use_openrouter if use_openrouter is not None else ("/" in failed_model)
        if is_or:
            fallback_models = settings.OPENROUTER_AVAILABLE_MODELS
        else:
            fallback_models = settings.AVAILABLE_MODELS

        for fallback_model in fallback_models:
            if fallback_model == failed_model:
                continue

            key_data, model_used, resolution = await use_case.resolve_ai_request(
                fallback_model, use_openrouter=use_openrouter,
            )
            if not key_data:
                continue

            logging.info(
                "Model fallback: trying %s instead of %s (all keys rejected by API for original model)",
                fallback_model, failed_model,
            )

            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"], history, model_used,
                system_instruction, user_id, chat_id, use_openrouter,
            )

            if response_text and not is_error_message(response_text):
                logging.info(
                    "Model fallback succeeded: %s → %s", failed_model, model_used,
                )
                try:
                    await status_mgr.record_success(key_data["key_hash"], model_used)
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)
                try:
                    await use_case.increment_key_usage(
                        key_data["key_hash"], model_used, use_openrouter,
                    )
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)
                return response_text, token_count

            logging.warning(
                "Model fallback %s also failed: %s",
                fallback_model, (response_text or "")[:100],
            )

        return None

    async def get_key_stats(self) -> list[dict[str, Any]]:
        """Return health stats for all tracked keys (for diagnostics)."""
        from app.repos.keys import get_key_status_manager
        return await get_key_status_manager().get_all_statuses()



# Module-level singleton
_provider_router: ProviderRouter | None = None


def get_provider_router() -> ProviderRouter:
    """Get the singleton ProviderRouter instance."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


async def get_ai_response(
    api_key: str,
    history: list[dict[str, Any]],
    model_name: str,
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    max_retries: int = 3,
) -> tuple[str, int | None]:
    """
    Unified entry point for AI responses.

    Automatically selects the appropriate provider based on model name.
    Returns tuple (response_text, token_count) for backwards compatibility.

    Args:
        api_key: API key for the provider
        history: Message history
        model_name: Model identifier (Gemini or OpenRouter format)
        system_instruction: Optional system prompt
        user_id: User ID for logging
        chat_id: Chat ID for logging
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (response_text, token_count)
    """
    provider = get_provider_for_model(model_name, api_key)

    response = await provider.get_response(
        history=history,
        model_name=model_name,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        max_retries=max_retries,
    )

    return response.text, response.token_count if response.success else None

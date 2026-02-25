"""
AI Provider abstraction layer for unified API interactions.

This module provides:
- BaseAIProvider abstract class defining common interface
- GeminiProvider and OpenRouterProvider implementations
- ProviderRouter with per-key health scoring and automatic failover
- Unified get_ai_response() function with automatic provider selection
- Common validation, retry logic, and error handling
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, Dict, Any, Set
from dataclasses import dataclass, field


from app.errors import user_friendly_error, is_error_message, is_key_related_error
from app.resilience_policy import ResiliencePolicy, run_with_resilience


@dataclass
class AIResponse:
    """Standardized response from any AI provider."""

    text: str
    token_count: int
    success: bool
    error_message: Optional[str] = None
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
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        max_retries: int = 3,
        timeout: float = 120.0,
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
        # Validate inputs
        self._validate_inputs(history, model_name, user_id, chat_id)

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
            )

        try:
            response, _ = await run_with_resilience(
                _operation,
                policy,
                circuit_name=f"ai_provider:{self.provider_name}",
                is_retryable=lambda e: self._is_transient_error(str(e)),
            )
            return response
        except Exception as e:
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
        history: List[Dict[str, Any]],
        model_name: str,
        user_id: Optional[int],
        chat_id: Optional[int],
    ) -> None:
        """Validate common input parameters."""
        if not isinstance(history, list) or not history:
            raise ValueError("history must be a non-empty list")

        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")

        if user_id is not None and not isinstance(user_id, int):
            raise ValueError("user_id must be an integer")

        if chat_id is not None and not isinstance(chat_id, int):
            raise ValueError("chat_id must be an integer")

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
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float,
    ) -> AIResponse:
        """
        Execute the actual API request. Must be implemented by subclasses.
        """
        pass

    def _categorize_error(self, error: Exception) -> str:
        """Categorize error for user-friendly message."""
        error_text = str(error).lower()

        if "quota" in error_text:
            return "🚫 Достигнут лимит запросов к API."
        elif (
            "503" in str(error)
            or "unavailable" in error_text
            or "overloaded" in error_text
        ):
            return "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
        elif "rate limit" in error_text:
            return "⏱️ Превышен лимит запросов в секунду. Подождите немного."
        elif "timeout" in error_text:
            return "⏰ Превышено время ожидания. Попробуйте позже."
        elif "invalid" in error_text or "malformed" in error_text:
            return "❌ Некорректный запрос. Проверьте параметры."
        elif "unauthorized" in error_text or "401" in str(error):
            return "🔑 Неверный API ключ."
        elif "402" in str(error):
            return "💳 Недостаточно средств на счету."
        else:
            return f"❌ Произошла ошибка: {error}"


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
    """Google Gemini AI provider."""

    provider_name = "gemini"

    async def _execute_request(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float,
    ) -> AIResponse:
        # Call the execution function directly — BaseAIProvider already handles retries
        from app.services import _execute_gemini_request

        text, tokens = await _execute_gemini_request(
            api_key=self.api_key,
            history=history,
            model_name=model_name,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
        )

        is_err = is_error_message(text)

        return AIResponse(
            text=text,
            token_count=tokens or 0,
            success=not is_err and tokens is not None,
            error_message=text if is_err else None,
            provider=self.provider_name,
            model=model_name,
        )




class OpenRouterProvider(BaseAIProvider):
    """OpenRouter AI provider."""

    provider_name = "openrouter"

    async def _execute_request(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float,
    ) -> AIResponse:
        # Call the execution function directly — BaseAIProvider already handles retries
        from app.services import _execute_openrouter_request

        text, tokens = await _execute_openrouter_request(
            api_key=self.api_key,
            history=history,
            model_name=model_name,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
        )

        is_err = is_error_message(text)

        return AIResponse(
            text=text,
            token_count=tokens or 0,
            success=not is_err and tokens is not None,
            error_message=text if is_err else None,
            provider=self.provider_name,
            model=model_name,
        )




# ── ProviderRouter ───────────────────────────────────────────────────────────


@dataclass
class KeyHealth:
    """Health score for an individual API key."""

    key_hash: str
    score: float = 1.0  # 1.0 = perfect health, 0.0 = dead
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    total_successes: int = 0
    total_failures: int = 0

    # Tuning knobs
    _DECAY_PER_FAILURE: float = 0.4  # multiplicative penalty per failure
    _RECOVERY_PER_SUCCESS: float = 0.15  # additive recovery per success
    _COOLDOWN_SECONDS: float = 30.0  # time before deprioritized key is retried

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.score = max(0.0, self.score * (1. - self._DECAY_PER_FAILURE))
        self.last_failure_time = time.monotonic()

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_successes += 1
        self.score = min(1.0, self.score + self._RECOVERY_PER_SUCCESS)

    @property
    def is_healthy(self) -> bool:
        """Key is healthy enough to try."""
        if self.score >= 0.3:
            return True
        # Allow retry after cooldown even if score is low
        elapsed = time.monotonic() - self.last_failure_time
        return elapsed >= self._COOLDOWN_SECONDS

def _has_multimodal_content(history: list) -> bool:
    """Detect if history contains multimodal (image) parts."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return False
    for message in history:
        parts = message.get("parts", [])
        for part in parts:
            if isinstance(part, PILImage.Image):
                return True
            if isinstance(part, (bytes, bytearray)):
                return True
    return False


class ProviderRouter:
    """
    Routes AI requests to the right provider with key rotation and health scoring.

    Wraps the existing AgentRequestUseCase logic while adding:
    - Per-key health tracking (exponential decay on failure, linear recovery)
    - Automatic key skipping for unhealthy keys
    - Cooldown-based recovery for deprioritized keys
    """

    def __init__(self, rate_limit_per_minute: int = 20) -> None:
        self._key_health: Dict[str, KeyHealth] = {}
        # Use the consolidated RateLimiter from security.py (includes periodic cleanup)
        from app.security import RateLimiter
        self._rate_limiter = RateLimiter(
            max_requests=rate_limit_per_minute, window_seconds=60
        )

    def _get_health(self, key_hash: str) -> KeyHealth:
        if key_hash not in self._key_health:
            self._key_health[key_hash] = KeyHealth(key_hash=key_hash)
        return self._key_health[key_hash]

    async def get_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        use_openrouter: Optional[bool] = None,
        max_key_retries: int = 3,
    ) -> Tuple[str, Optional[int]]:
        """
        Get AI response with automatic key rotation and health-aware selection.

        Delegates to AgentRequestUseCase for key resolution, but wraps it
        with health tracking so persistently-failing keys are deprioritized.
        """
        from app.agent_use_cases import AgentRequestUseCase

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
        failed_keys: Set[str] = set()

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
                        f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже.",
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                        None,
                    )
                return (
                    "🚫 Не удалось получить доступный ключ API. Попробуйте позже.",
                    None,
                )

            # Skip unhealthy keys unless this is our last resort
            health = self._get_health(key_data["key_hash"])
            if not health.is_healthy and attempt < max_key_retries - 1:
                failed_keys.add(key_data["key_hash"])
                logging.debug(
                    f"Skipping unhealthy key {key_data['key_hash'][:8]}... "
                    f"(score={health.score:.2f})"
                )
                continue

            # Execute the request
            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
            )

            # Track health based on response
            if (
                response_text
                and is_error_message(response_text)
                and is_key_related_error(response_text)
            ):
                health.record_failure()
                failed_keys.add(key_data["key_hash"])
                logging.warning(
                    f"Key {key_data['key_hash'][:8]}... failed "
                    f"(score={health.score:.2f}, attempt {attempt + 1}/{max_key_retries}). "
                    f"Error: {response_text[:100]}..."
                )
                continue

            # Success — update health and increment usage
            if response_text and not is_error_message(response_text):
                health.record_success()
                await use_case.increment_key_usage(
                    key_data["key_hash"], model_used, use_openrouter
                )

            return response_text, token_count

        is_or = (
            use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        )
        provider_name = "OpenRouter" if is_or else "Gemini"
        return (
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            None,
        )

    def get_key_stats(self) -> Dict[str, Dict[str, Any]]:
        """Return health stats for all tracked keys (for diagnostics)."""
        return {
            kh.key_hash[:8]: {
                "score": round(kh.score, 3),
                "consecutive_failures": kh.consecutive_failures,
                "total_successes": kh.total_successes,
                "total_failures": kh.total_failures,
                "is_healthy": kh.is_healthy,
            }
            for kh in self._key_health.values()
        }


# Module-level singleton
_provider_router: Optional[ProviderRouter] = None


def get_provider_router() -> ProviderRouter:
    """Get the singleton ProviderRouter instance."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


async def get_ai_response(
    api_key: str,
    history: List[Dict[str, Any]],
    model_name: str,
    system_instruction: Optional[str] = None,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    max_retries: int = 3,
) -> Tuple[str, Optional[int]]:
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

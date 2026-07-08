"""
AI Provider base classes and shared utilities.

Provides:
- AIResponse dataclass for standardized responses
- BaseAIProvider ABC with retry logic and validation
- Thinking config helpers for Gemini models
- Factory function get_provider_for_model()
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from google.genai import types

from app.errors import user_friendly_error
from app.resilience_policy import ResiliencePolicy, run_with_resilience

# ── Thinking config helpers ──────────────────────────────────────────

_THINKING_BUDGET_MAP = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}
_THINKING_LEVEL_MAP: dict[str, str] = {
    "off": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _is_gemini3_model(model_name: str) -> bool:
    """Detect Gemini 3.x models that require thinkingLevel instead of thinkingBudget."""
    return "gemini-3" in model_name


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
        return types.ThinkingConfig(thinking_level=_THINKING_LEVEL_MAP[thinking_level])  # type: ignore[arg-type]  # Pydantic coerces str→ThinkingLevel
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
            from app.resilience_policy import is_retryable_exception

            def custom_is_retryable(e: Exception) -> bool:
                if is_retryable_exception(e):
                    return True
                return self._is_transient_error(str(e))

            response, _ = await run_with_resilience(
                _operation,
                policy,
                circuit_name=f"ai_provider:{self.provider_name}",
                is_retryable=custom_is_retryable,
            )
            return response
        except Exception as e:
            # Catches APIError, httpx.HTTPError, CircuitBreakerOpenError, and any
            # other exception that escapes run_with_resilience after all retries,
            # converting it into a structured AIResponse instead of an unhandled
            # background-task exception.
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
            return "user_id must be an integer"  # type: ignore[unreachable]  # defensive

        if chat_id is not None and not isinstance(chat_id, int):
            return "chat_id must be an integer"  # type: ignore[unreachable]  # defensive

        return None

    def _is_transient_error(self, error_text: str) -> bool:
        """Check if error is transient and can be retried."""
        transient_patterns = [
            "503",
            "504",
            "deadline",
            "unavailable",
            "overloaded",
            "rate limit",
            "timeout",
            "connection",
            "temporarily",
        ]
        error_lower = error_text.lower()
        return any(pattern in error_lower for pattern in transient_patterns)

    # ── Shared logging helpers (AR-3 dedup) ──────────────────────────────

    def _error_response(
        self,
        msg: str,
        model: str,
        start_time,
        user_id,
        chat_id,
    ) -> AIResponse:
        logging.error(msg)
        self._log_failure(start_time, model, msg, user_id, chat_id)
        return AIResponse(
            text=f"❌ {msg}",
            token_count=0,
            success=False,
            error_message=msg,
            provider=self.provider_name,
            model=model,
        )

    @abstractmethod
    def _log_failure(self, start_time, model, msg, user_id, chat_id):
        """Log a failed API call. Subclasses override for provider-specific logging."""

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

    @abstractmethod
    async def stream_response(
        self,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None = None,
        thinking_level: str | None = None,
        timeout: float = 120.0,
        enable_web_search: bool = False,
        force_grounding: bool = False,
    ) -> None:  # type: ignore[override]  # subclasses yield — AsyncGenerator[Any, None]
        """
        Stream response from AI provider. Must be implemented by subclasses.
        Yields chunks of text as they arrive.

        Args:
            enable_web_search: If True, enable native web search grounding
                (Google Search for Gemini, ignored by other providers).
            force_grounding: If True, use DynamicRetrievalConfig with threshold=0.0
                so the model ALWAYS performs a web search instead of relying on
                dynamic retrieval heuristics. Use for inline / real-time queries.
        """



# Known FreeTheAI routing prefixes — checked BEFORE the generic "/" detection
# to prevent FTA models from being misrouted to OpenRouter.
KNOWN_FTA_PREFIXES: tuple[str, ...] = (
    "cat/",
    "yng/",
    "vhr/",
    "img/",          # img/gpt-image-2 — FTA image generation endpoint
    "or/google/lyria-",
)


def is_freetheai_model(model_name: str) -> bool:
    """Check if model name indicates a FreeTheAI model."""
    if not model_name:
        return False
    return any(model_name.startswith(p) for p in KNOWN_FTA_PREFIXES)


def is_openrouter_model(model_name: str) -> bool:
    """Check if model name indicates an OpenRouter model.

    Has '/' but is NOT opencode-go/ and NOT a FreeTheAI prefix.
    """
    if not model_name:
        return False
    return (
        "/" in model_name
        and not model_name.startswith("opencode-go/")
        and not is_freetheai_model(model_name)
    )


def is_opencode_model(model_name: str) -> bool:
    """Check if model name indicates an Opencode Go model."""
    if not model_name:
        return False
    return model_name.startswith("opencode-go/")


def is_vertex_provider_key(api_key: str) -> bool:
    """Check if the provided api_key is the pseudo-key for Vertex AI."""
    return api_key == "vertex"


def get_provider_for_model(model_name: str, api_key: str) -> BaseAIProvider:
    """
    Factory function to get appropriate provider for a model.

    Dispatch order (important — multiple providers use '/' in model names):
    1. ``opencode-go/*``     → ``OpencodeGoProvider``
    2. FreeTheAI prefixes    → ``FreeTheAIProvider``
    3. ``org/model``         → ``OpenRouterProvider``
    4. Vertex pseudo-key     → ``VertexGeminiProvider``
    5. everything else       → ``GeminiProvider``

    Args:
        model_name: Model identifier
        api_key: API key for the provider

    Returns:
        Appropriate AIProvider instance
    """
    from app.providers.freetheai import FreeTheAIProvider
    from app.providers.gemini import GeminiProvider, VertexGeminiProvider
    from app.providers.opencode import OpencodeGoProvider
    from app.providers.openrouter import OpenRouterProvider

    if is_opencode_model(model_name):
        return OpencodeGoProvider(api_key)
    elif is_freetheai_model(model_name):
        return FreeTheAIProvider(api_key)
    elif is_openrouter_model(model_name):
        return OpenRouterProvider(api_key)
    elif is_vertex_provider_key(api_key):
        return VertexGeminiProvider()
    else:
        return GeminiProvider(api_key)

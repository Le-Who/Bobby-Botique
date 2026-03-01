"""
Unified error handling for GemAI Bot v2.

This module provides:
- Typed exception hierarchy (base + domain-specific)
- User-friendly error message constants
- Error classification functions (retryable, key-related)
- APIError exception class with auto-detection
- handle_api_errors async context manager for standardized handling
- Keyboard builders for error recovery UI
"""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message

# =============================================================================
# TYPED EXCEPTION HIERARCHY
# =============================================================================


class GemaibotBaseException(Exception):
    """Base exception class for all bot-related errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


# --- Database Exceptions ---


class DatabaseError(GemaibotBaseException):
    """Base class for database-related errors."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails."""
    pass


class DatabaseQueryError(DatabaseError):
    """Raised when database query execution fails."""
    pass


class DatabaseRateLimitError(DatabaseError):
    """Raised when database rate limit is exceeded."""
    pass


class DatabasePoolError(DatabaseError):
    """Raised when database connection pool issues occur."""
    pass


# --- API Exceptions ---


class GemaibotAPIError(GemaibotBaseException):
    """Base class for API-related errors. Renamed to avoid collision with google.genai.errors.APIError."""
    pass


class GeminiAPIError(GemaibotAPIError):
    """Raised when Gemini API calls fail."""
    pass


class TavilyAPIError(GemaibotAPIError):
    """Raised when Tavily API calls fail."""
    pass


class TelegramAPIError(GemaibotAPIError):
    """Raised when Telegram Bot API calls fail."""
    pass


class APIQuotaExceededError(GemaibotAPIError):
    """Raised when API quota is exceeded."""
    pass


class APIInvalidResponseError(GemaibotAPIError):
    """Raised when API returns invalid response."""
    pass


# --- Network Exceptions ---


class NetworkError(GemaibotBaseException):
    """Base class for network-related errors."""
    pass


class ConnectionTimeoutError(NetworkError):
    """Raised when connection times out."""
    pass


class ServiceConnectionRefusedError(NetworkError):
    """Raised when connection is refused. Renamed to avoid shadowing built-in."""
    pass


class CircuitBreakerOpenError(NetworkError):
    """Raised when circuit breaker is open."""
    pass


# --- Validation Exceptions ---


class ValidationError(GemaibotBaseException):
    """Base class for validation errors."""
    pass


class InputValidationError(ValidationError):
    """Raised when input validation fails."""
    pass


class ConfigurationError(ValidationError):
    """Raised when configuration is invalid."""
    pass


# --- Business Logic Exceptions ---


class BusinessLogicError(GemaibotBaseException):
    """Base class for business logic errors."""
    pass


class UserLimitExceededError(BusinessLogicError):
    """Raised when user limits are exceeded."""
    pass


class DocumentProcessingError(BusinessLogicError):
    """Raised when document processing fails."""
    pass


class ChatStateError(BusinessLogicError):
    """Raised when chat state operations fail."""
    pass


# --- Cache Exceptions ---


class CacheError(GemaibotBaseException):
    """Base class for cache-related errors."""
    pass


class RedisConnectionError(CacheError):
    """Raised when Redis connection fails."""
    pass


class CacheKeyError(CacheError):
    """Raised when cache key operations fail."""
    pass


# --- Security Exceptions ---


class SecurityError(GemaibotBaseException):
    """Base class for security-related errors."""
    pass


class InputSanitizationError(SecurityError):
    """Raised when input sanitization fails."""
    pass


class AuthenticationError(SecurityError):
    """Raised when authentication fails."""
    pass


class DecryptionError(SecurityError):
    """Raised when API key decryption fails (e.g. ADMIN_SECRET mismatch)."""
    pass


# =============================================================================
# EXCEPTION CONVERSION UTILITY
# =============================================================================


def convert_to_typed_exception(
    exception: Exception, context: str = ""
) -> GemaibotBaseException:
    """Converts generic exceptions to typed exceptions based on context."""

    error_message = str(exception)
    error_type = type(exception).__name__

    # Database exceptions
    if "asyncpg" in error_type or "postgres" in error_message.lower():
        if "connection" in error_message.lower() or "timeout" in error_message.lower():
            return DatabaseConnectionError(
                f"Database connection failed: {error_message}",
                {"original_error": error_type, "context": context},
            )
        elif "rate limit" in error_message.lower() or "quota" in error_message.lower():
            return DatabaseRateLimitError(
                f"Database rate limit exceeded: {error_message}",
                {"original_error": error_type, "context": context},
            )
        else:
            return DatabaseQueryError(
                f"Database query failed: {error_message}",
                {"original_error": error_type, "context": context},
            )

    # Network exceptions
    elif "httpx" in error_type or "connection" in error_message.lower():
        if "timeout" in error_message.lower():
            return ConnectionTimeoutError(
                f"Connection timeout: {error_message}",
                {"original_error": error_type, "context": context},
            )
        elif "refused" in error_message.lower():
            return ServiceConnectionRefusedError(
                f"Connection refused: {error_message}",
                {"original_error": error_type, "context": context},
            )
        else:
            return NetworkError(
                f"Network error: {error_message}",
                {"original_error": error_type, "context": context},
            )

    # API exceptions
    elif "api" in error_message.lower() or "gemini" in error_message.lower():
        if "quota" in error_message.lower() or "limit" in error_message.lower():
            return APIQuotaExceededError(
                f"API quota exceeded: {error_message}",
                {"original_error": error_type, "context": context},
            )
        else:
            return GemaibotAPIError(
                f"API error: {error_message}",
                {"original_error": error_type, "context": context},
            )

    # Default fallback
    return GemaibotBaseException(
        f"Unexpected error: {error_message}",
        {"original_error": error_type, "context": context},
    )


# =============================================================================
# USER-FRIENDLY ERROR MESSAGES (единая точка кастомизации)
# =============================================================================
GENERIC_ERROR = "❌ Произошла ошибка. Попробуйте ещё раз."
OVERLOADED_ERROR = "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
QUOTA_ERROR = "🚫 Достигнут лимит запросов к API."
PROCESSING_ERROR = "❌ Ошибка обработки запроса."
DOCUMENT_ERROR = "❌ Ошибка обработки содержимого документа."
TIMEOUT_ERROR = "⏰ Превышено время ожидания. Попробуйте ещё раз."
NETWORK_ERROR_MSG = "🌐 Ошибка сети. Проверьте подключение."


# =============================================================================
# ERROR CLASSIFICATION FUNCTIONS
# =============================================================================


def user_friendly_error(raw_error: Exception | str) -> str:
    """Возвращает короткое дружелюбное сообщение для пользователя."""
    text = str(raw_error) if isinstance(raw_error, Exception) else raw_error
    low = (text or "").lower()
    if any(x in low for x in ["503", "unavailable", "overloaded"]):
        return OVERLOADED_ERROR
    if "quota" in low or "limit" in low:
        return QUOTA_ERROR
    if "timeout" in low or "timed out" in low:
        return TIMEOUT_ERROR
    return GENERIC_ERROR


def is_error_message(text: str) -> bool:
    """Определяет, является ли сообщение ошибкой по наличию эмодзи ошибок."""
    if not text:
        return False
    error_indicators = ["⏰", "❌", "🔄", "🚫", "⏱️", "💳", "🌐", "🔑"]
    return any(text.startswith(indicator) for indicator in error_indicators)


def is_retryable_error(text: str) -> bool:
    """Определяет, можно ли повторить запрос при этой ошибке."""
    if not text:
        return False
    # Временные ошибки, которые можно повторить
    retryable_patterns = [
        "⏰",  # Таймаут
        "🔄",  # Перегрузка сервера
        "⏱️",  # Rate limit
        "🌐",  # Сетевая ошибка
        "Превышено время ожидания",
        "перегружен",
        "rate limit",
        "503",
        "unavailable",
        "overloaded",
        "timeout",
        "timed out",
    ]
    text_lower = text.lower()
    return any(
        pattern.lower() in text_lower or text.startswith(pattern)
        for pattern in retryable_patterns
    )


def is_key_related_error(text: str) -> bool:
    """
    Определяет, является ли ошибка связанной с ключом API.
    Такие ошибки требуют попытки с другим ключом.
    """
    if not text:
        return False

    text_lower = text.lower()

    # Ошибки, связанные с ключом - пробуем другой ключ
    key_related_patterns = [
        "🚫",  # Quota/лимит
        "⏱️",  # Rate limit
        "🔑",  # Invalid API key
        "quota",
        "quota exceeded",
        "rate limit",
        "rate_limit",
        "daily limit",
        "limit exceeded",
        "invalid api key",
        "authentication",
        "unauthorized",
        "forbidden",
        "api key",
        "api_key",
        "достигнут лимит",
        "превышен лимит",
        "лимит запросов",
    ]

    # Ошибки, НЕ связанные с ключом - не меняем ключ
    not_key_related_patterns = [
        "⏰",  # Timeout
        "🔄",  # Service unavailable (503)
        "503",
        "unavailable",
        "overloaded",
        "timeout",
        "превышено время ожидания",
        "перегружен",
        "некорректный запрос",
        "invalid request",
        "malformed",
    ]

    # Сначала проверяем на ошибки, НЕ связанные с ключом (приоритет выше)
    if any(
        pattern.lower() in text_lower or text.startswith(pattern)
        for pattern in not_key_related_patterns
    ):
        return False

    # Проверка на ошибки, связанные с ключом
    return any(
        pattern.lower() in text_lower or text.startswith(pattern)
        for pattern in key_related_patterns
    )


def classify_key_error(text: str) -> str:
    """Classify a key-related error into a penalty category.

    Returns one of:
        "permanent"  – API_KEY_INVALID, auth errors → long cooldown (24 h)
        "quota"      – daily quota exhausted → suspend until midnight PT
        "rate_limit" – per-second/minute rate limit → short cooldown (60 s)
        "transient"  – not key-related at all (503, timeout) → no suspension
    """
    if not text:
        return "transient"

    text_lower = text.lower()

    # Transient / non-key errors — highest priority
    transient_patterns = [
        "⏰", "🔄", "503", "unavailable", "overloaded",
        "timeout", "timed out", "превышено время ожидания",
        "перегружен", "некорректный запрос", "invalid request",
        "malformed",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in transient_patterns):
        return "transient"

    # Permanent key errors — invalid key / auth
    permanent_patterns = [
        "🔑", "api_key_invalid", "invalid api key",
        "authentication", "unauthorized", "forbidden",
        "неверный api ключ",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in permanent_patterns):
        return "permanent"

    # Quota / daily limit errors
    quota_patterns = [
        "🚫", "quota", "quota exceeded", "daily limit",
        "limit exceeded", "достигнут лимит",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in quota_patterns):
        return "quota"

    # Rate-limit errors
    rate_patterns = [
        "⏱️", "rate limit", "rate_limit",
        "превышен лимит", "лимит запросов",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in rate_patterns):
        return "rate_limit"

    return "transient"


# =============================================================================
# KEYBOARD BUILDERS
# =============================================================================


def build_retry_and_roles_keyboard(include_roles: bool = True) -> InlineKeyboardMarkup:
    """Builds keyboard with retry button and optional roles button."""
    buttons = [
        [InlineKeyboardButton("🔁 Попробовать ещё раз", callback_data="retry_last")]
    ]
    if include_roles:
        buttons.append(
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")]
        )
    return InlineKeyboardMarkup(buttons)


def build_roles_keyboard() -> InlineKeyboardMarkup:
    """Builds keyboard with only roles button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")]]
    )


# =============================================================================
# LOGGING HELPERS
# =============================================================================


def log_and_format_error(context: str, err: Exception) -> str:
    """Logs error with context and returns user-friendly message."""
    logging.error("%s: %s", context, err, exc_info=True)
    return user_friendly_error(err)


# =============================================================================
# APIError EXCEPTION CLASS (Telegram-facing)
# =============================================================================


class APIError(GemaibotAPIError):
    """
    Exception for API-related errors with user-friendly messages.

    Extends GemaibotAPIError from the typed hierarchy.

    Attributes:
        raw_error: Original error text or exception
        retryable: Whether the error can be resolved by retrying
        key_related: Whether the error requires trying a different API key
        user_message: User-friendly error message
    """

    def __init__(
        self,
        raw_error: str | Exception,
        retryable: bool = False,
        key_related: bool = False,
    ):
        self.raw_error = (
            str(raw_error) if isinstance(raw_error, Exception) else raw_error
        )
        self.retryable = retryable
        self.key_related = key_related
        self.user_message = user_friendly_error(self.raw_error)
        # Initialize base with message + empty details
        super().__init__(self.user_message)

    @classmethod
    def from_exception(cls, exc: Exception) -> "APIError":
        """Create APIError from any exception, auto-detecting error type."""
        raw = str(exc)
        return cls(
            raw_error=raw,
            retryable=is_retryable_error(raw),
            key_related=is_key_related_error(raw),
        )


# =============================================================================
# ASYNC CONTEXT MANAGER FOR UNIFIED ERROR HANDLING
# =============================================================================


@asynccontextmanager
async def handle_api_errors(
    placeholder_message: Message,
    context: str = "API request",
    on_error: Callable[[Exception], Any] | None = None,
    show_retry_button: bool = True,
    reraise: bool = False,
):
    """
    Async context manager for unified API error handling.

    Automatically:
    - Logs the error with context
    - Displays user-friendly message in the placeholder
    - Shows appropriate retry/roles keyboard
    - Calls optional error callback
    """
    try:
        yield
    except Exception as e:
        error_text = str(e)
        user_msg = user_friendly_error(e)

        # Log the error with context
        logging.error("%s: %s", context, e, exc_info=True)

        # Determine keyboard
        keyboard = None
        if show_retry_button and is_retryable_error(error_text):
            keyboard = build_retry_and_roles_keyboard(include_roles=True)
        elif show_retry_button:
            keyboard = build_roles_keyboard()

        # Update the message
        try:
            await placeholder_message.edit_text(user_msg, reply_markup=keyboard)
        except Exception as edit_error:
            logging.warning("Failed to edit message with error: %s", edit_error)

        # Run callback if provided
        if on_error:
            try:
                result = on_error(e)
                if hasattr(result, "__await__"):
                    await result
            except Exception as callback_error:
                logging.warning("Error callback failed: %s", callback_error)

        if reraise:
            raise

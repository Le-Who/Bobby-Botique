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
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from enum import StrEnum
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


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails."""


class DatabaseQueryError(DatabaseError):
    """Raised when database query execution fails."""


class DatabaseRateLimitError(DatabaseError):
    """Raised when database rate limit is exceeded."""


class DatabasePoolError(DatabaseError):
    """Raised when database connection pool issues occur."""


# --- API Exceptions ---


class GemaibotAPIError(GemaibotBaseException):
    """Base class for API-related errors. Renamed to avoid collision with google.genai.errors.APIError."""


class GeminiAPIError(GemaibotAPIError):
    """Raised when Gemini API calls fail."""


class TavilyAPIError(GemaibotAPIError):
    """Raised when Tavily API calls fail."""


class TelegramAPIError(GemaibotAPIError):
    """Raised when Telegram Bot API calls fail."""


class APIQuotaExceededError(GemaibotAPIError):
    """Raised when API quota is exceeded."""


class APIInvalidResponseError(GemaibotAPIError):
    """Raised when API returns invalid response."""


# --- Network Exceptions ---


class NetworkError(GemaibotBaseException):
    """Base class for network-related errors."""


class ConnectionTimeoutError(NetworkError):
    """Raised when connection times out."""


class ServiceConnectionRefusedError(NetworkError):
    """Raised when connection is refused. Renamed to avoid shadowing built-in."""


class CircuitBreakerOpenError(NetworkError):
    """Raised when circuit breaker is open."""


class ProviderOverloadError(NetworkError):
    """Raised when all AI providers are overloaded/unavailable.

    Distinct from ValueError (unintelligible category) — this means the
    infrastructure is temporarily down, not that the user's input is bad.
    """


# --- Validation Exceptions ---


class ValidationError(GemaibotBaseException):
    """Base class for validation errors."""


class InputValidationError(ValidationError):
    """Raised when input validation fails."""


class ConfigurationError(ValidationError):
    """Raised when configuration is invalid."""


# --- Business Logic Exceptions ---


class BusinessLogicError(GemaibotBaseException):
    """Base class for business logic errors."""


class UserLimitExceededError(BusinessLogicError):
    """Raised when user limits are exceeded."""


class DocumentProcessingError(BusinessLogicError):
    """Raised when document processing fails."""


class ChatStateError(BusinessLogicError):
    """Raised when chat state operations fail."""


# --- Cache Exceptions ---


class CacheError(GemaibotBaseException):
    """Base class for cache-related errors."""


class RedisConnectionError(CacheError):
    """Raised when Redis connection fails."""


class CacheKeyError(CacheError):
    """Raised when cache key operations fail."""


# --- Security Exceptions ---


class SecurityError(GemaibotBaseException):
    """Base class for security-related errors."""


class InputSanitizationError(SecurityError):
    """Raised when input sanitization fails."""


class AuthenticationError(SecurityError):
    """Raised when authentication fails."""


class DecryptionError(SecurityError):
    """Raised when API key decryption fails (e.g. ADMIN_SECRET mismatch)."""


# =============================================================================
# EXCEPTION CONVERSION UTILITY
# =============================================================================


def convert_to_typed_exception(exception: Exception, context: str = "") -> GemaibotBaseException:
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
# ERROR CODES — structured classification (replaces emoji-based text parsing)
# =============================================================================


class ErrorCode(StrEnum):
    """Structured error codes for deterministic classification.

    Each code maps to fixed properties (retryable, key-related, penalty category)
    via _ERROR_PROPERTIES, eliminating fragile emoji/text pattern matching.
    """

    # Transient / retryable
    TIMEOUT = "TIMEOUT"
    OVERLOADED = "OVERLOADED"  # 503, server busy
    NETWORK = "NETWORK"  # connection errors
    RATE_LIMIT = "RATE_LIMIT"  # per-second/minute throttle

    # Key-related
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"  # daily quota exhausted
    INVALID_KEY = "INVALID_KEY"  # API key rejected
    KEYS_EXHAUSTED = "KEYS_EXHAUSTED"  # all keys tried, none worked
    DECRYPTION_FAILED = "DECRYPTION_FAILED"  # ADMIN_SECRET mismatch
    NO_KEYS = "NO_KEYS"  # provider not configured

    # Non-retryable
    INVALID_REQUEST = "INVALID_REQUEST"  # malformed input
    INVALID_RESPONSE = "INVALID_RESPONSE"  # API returned garbage
    EMPTY_RESPONSE = "EMPTY_RESPONSE"  # API returned nothing
    PROCESSING = "PROCESSING"  # general processing failure
    DOCUMENT = "DOCUMENT"  # document parsing failure
    GENERIC = "GENERIC"  # catch-all
    USER_RATE_LIMIT = "USER_RATE_LIMIT"  # per-user throttle (not key-related)


# Properties: (retryable, key_related, penalty_category)
_ERROR_PROPERTIES: dict[ErrorCode, tuple[bool, bool, str]] = {
    ErrorCode.TIMEOUT: (True, False, "transient"),
    ErrorCode.OVERLOADED: (True, False, "transient"),
    ErrorCode.NETWORK: (True, False, "transient"),
    ErrorCode.RATE_LIMIT: (True, True, "rate_limit"),
    ErrorCode.QUOTA_EXCEEDED: (False, True, "quota"),
    ErrorCode.INVALID_KEY: (False, True, "permanent"),
    ErrorCode.KEYS_EXHAUSTED: (False, True, "quota"),
    ErrorCode.DECRYPTION_FAILED: (False, False, "permanent"),
    ErrorCode.NO_KEYS: (False, False, "permanent"),
    ErrorCode.INVALID_REQUEST: (False, False, "transient"),
    ErrorCode.INVALID_RESPONSE: (False, False, "transient"),
    ErrorCode.EMPTY_RESPONSE: (False, False, "transient"),
    ErrorCode.PROCESSING: (False, False, "transient"),
    ErrorCode.DOCUMENT: (False, False, "transient"),
    ErrorCode.GENERIC: (False, False, "transient"),
    ErrorCode.USER_RATE_LIMIT: (False, False, "transient"),
}

# Invisible tag prefix: Zero-Width Space + code in brackets.
# Invisible to users in Telegram but extractable by code.
_TAG_PREFIX = "\u200b["  # ​[
_TAG_SUFFIX = "]"


def tag_error(code: ErrorCode, message: str) -> str:
    """Tag a user-facing error message with a machine-readable error code.

    The code is embedded as an invisible prefix (zero-width space + brackets)
    so it doesn't affect display in Telegram but enables O(1) classification.
    """
    return f"{_TAG_PREFIX}{code.value}{_TAG_SUFFIX}{message}"


def extract_error_code(text: str) -> ErrorCode | None:
    """Extract the ErrorCode from a tagged error message, or None if untagged."""
    if not text or not text.startswith(_TAG_PREFIX):
        return None
    end = text.find(_TAG_SUFFIX, len(_TAG_PREFIX))
    if end == -1:
        return None
    code_str = text[len(_TAG_PREFIX) : end]
    try:
        return ErrorCode(code_str)
    except ValueError:
        return None


def strip_error_tag(text: str) -> str:
    """Remove the invisible error code tag, returning clean user-facing text."""
    if not text or not text.startswith(_TAG_PREFIX):
        return text
    end = text.find(_TAG_SUFFIX, len(_TAG_PREFIX))
    if end == -1:
        return text
    return text[end + len(_TAG_SUFFIX) :]


# =============================================================================
# USER-FRIENDLY ERROR MESSAGES (tagged with codes)
# =============================================================================
GENERIC_ERROR = tag_error(ErrorCode.GENERIC, "❌ Произошла ошибка. Попробуйте ещё раз.")
OVERLOADED_ERROR = tag_error(
    ErrorCode.OVERLOADED,
    "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд.",
)
QUOTA_ERROR = tag_error(ErrorCode.QUOTA_EXCEEDED, "🚫 Достигнут лимит запросов к API.")
PROCESSING_ERROR = tag_error(ErrorCode.PROCESSING, "❌ Ошибка обработки запроса.")
DOCUMENT_ERROR = tag_error(ErrorCode.DOCUMENT, "❌ Ошибка обработки содержимого документа.")
TIMEOUT_ERROR = tag_error(ErrorCode.TIMEOUT, "⏰ Превышено время ожидания. Попробуйте ещё раз.")
NETWORK_ERROR_MSG = tag_error(ErrorCode.NETWORK, "🌐 Ошибка сети. Проверьте подключение.")


# =============================================================================
# ERROR CLASSIFICATION FUNCTIONS — code-based with text fallback
# =============================================================================

# Exception type → ErrorCode mapping (O(1) classification)
_EXCEPTION_TYPE_MAP: dict[type, ErrorCode] = {
    DatabaseConnectionError: ErrorCode.TIMEOUT,
    DatabaseRateLimitError: ErrorCode.RATE_LIMIT,
    DatabaseQueryError: ErrorCode.PROCESSING,
    DatabasePoolError: ErrorCode.TIMEOUT,
    ConnectionTimeoutError: ErrorCode.TIMEOUT,
    ServiceConnectionRefusedError: ErrorCode.NETWORK,
    CircuitBreakerOpenError: ErrorCode.OVERLOADED,
    APIQuotaExceededError: ErrorCode.QUOTA_EXCEEDED,
    APIInvalidResponseError: ErrorCode.INVALID_RESPONSE,
    GeminiAPIError: ErrorCode.PROCESSING,
    TavilyAPIError: ErrorCode.NETWORK,
    NetworkError: ErrorCode.NETWORK,
    InputValidationError: ErrorCode.INVALID_REQUEST,
    DocumentProcessingError: ErrorCode.DOCUMENT,
    UserLimitExceededError: ErrorCode.USER_RATE_LIMIT,
    DecryptionError: ErrorCode.DECRYPTION_FAILED,
    AuthenticationError: ErrorCode.INVALID_KEY,
    # Python programmer-error builtins: code bugs → PROCESSING (not GENERIC)
    UnboundLocalError: ErrorCode.PROCESSING,
    AttributeError: ErrorCode.PROCESSING,
    TypeError: ErrorCode.PROCESSING,
    NameError: ErrorCode.PROCESSING,
}

# HTTP status code → ErrorCode mapping
_STATUS_CODE_MAP: dict[int, ErrorCode] = {
    429: ErrorCode.RATE_LIMIT,
    503: ErrorCode.OVERLOADED,
    502: ErrorCode.NETWORK,
    504: ErrorCode.TIMEOUT,
    401: ErrorCode.INVALID_KEY,
    403: ErrorCode.INVALID_KEY,
    400: ErrorCode.INVALID_REQUEST,
    500: ErrorCode.PROCESSING,
}

# ErrorCode → user-facing message
_ERROR_CODE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.TIMEOUT: TIMEOUT_ERROR,
    ErrorCode.OVERLOADED: OVERLOADED_ERROR,
    ErrorCode.NETWORK: NETWORK_ERROR_MSG,
    ErrorCode.RATE_LIMIT: QUOTA_ERROR,
    ErrorCode.QUOTA_EXCEEDED: QUOTA_ERROR,
    ErrorCode.INVALID_KEY: tag_error(ErrorCode.INVALID_KEY, "🔑 Ошибка API ключа. Обратитесь к администратору."),
    ErrorCode.KEYS_EXHAUSTED: tag_error(ErrorCode.KEYS_EXHAUSTED, "🔑 Все ключи исчерпаны. Попробуйте позже."),
    ErrorCode.DECRYPTION_FAILED: tag_error(ErrorCode.DECRYPTION_FAILED, "🔐 Ошибка дешифровки ключей."),
    ErrorCode.NO_KEYS: tag_error(ErrorCode.NO_KEYS, "⚙️ Провайдер не настроен."),
    ErrorCode.INVALID_REQUEST: tag_error(ErrorCode.INVALID_REQUEST, "❌ Некорректный запрос."),
    ErrorCode.INVALID_RESPONSE: tag_error(ErrorCode.INVALID_RESPONSE, "❌ Некорректный ответ от API."),
    ErrorCode.EMPTY_RESPONSE: tag_error(ErrorCode.EMPTY_RESPONSE, "❌ Пустой ответ от модели."),
    ErrorCode.PROCESSING: PROCESSING_ERROR,
    ErrorCode.DOCUMENT: DOCUMENT_ERROR,
    ErrorCode.USER_RATE_LIMIT: tag_error(ErrorCode.USER_RATE_LIMIT, "⏱️ Превышен лимит запросов. Подождите."),
    ErrorCode.GENERIC: GENERIC_ERROR,
}

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[_ -]?after\s*[=:]?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE),
    re.compile(r"retrydelay['\"]?\s*[:=]\s*['\"]?([0-9]+(?:\.[0-9]+)?)s?", re.IGNORECASE),
    re.compile(r"please retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE),
    re.compile(r"(?:около|about)\s+([0-9]+(?:\.[0-9]+)?)\s*[sс]", re.IGNORECASE),
)


def extract_retry_after_seconds(text: str) -> int | None:
    """Extract a retry-after hint from provider error text when available."""
    if not text:
        return None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return max(1, int(float(match.group(1)) + 0.999))
        except ValueError:
            continue
    return None


def has_retry_after_hint(text: str) -> bool:
    return extract_retry_after_seconds(text) is not None


def classify_error_from_exception(exc: Exception) -> ErrorCode:
    """Classify an exception to an ErrorCode by type (O(1) MRO walk).

    Falls back to string-based detection for unregistered exception types.
    """
    # Direct type match
    exc_type = type(exc)
    if exc_type in _EXCEPTION_TYPE_MAP:
        return _EXCEPTION_TYPE_MAP[exc_type]

    # MRO walk for subclass matches
    for base in exc_type.__mro__:
        if base in _EXCEPTION_TYPE_MAP:
            return _EXCEPTION_TYPE_MAP[base]

    # HTTP status code extraction (google.genai.errors.APIError, httpx, etc.)
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and status in _STATUS_CODE_MAP:
        return _STATUS_CODE_MAP[status]

    # String fallback for unknown exception types
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return ErrorCode.TIMEOUT
    if "503" in msg or "overloaded" in msg or "unavailable" in msg:
        return ErrorCode.OVERLOADED
    if has_retry_after_hint(msg):
        return ErrorCode.RATE_LIMIT
    if "429" in msg or "rate" in msg:
        return ErrorCode.RATE_LIMIT
    if "quota" in msg or "limit" in msg:
        return ErrorCode.QUOTA_EXCEEDED

    return ErrorCode.GENERIC


def classify_error_from_status_code(status_code: int) -> ErrorCode:
    """Classify an HTTP status code to an ErrorCode."""
    return _STATUS_CODE_MAP.get(status_code, ErrorCode.GENERIC)


def user_friendly_error(raw_error: Exception | str) -> str:
    """Возвращает короткое дружелюбное сообщение для пользователя.

    Uses typed exception classification first, falls back to text matching.
    """
    # Type-based classification (primary path)
    if isinstance(raw_error, Exception):
        code = classify_error_from_exception(raw_error)
        return _ERROR_CODE_MESSAGES.get(code, GENERIC_ERROR)

    # Text-based classification (legacy path)
    text = raw_error
    low = (text or "").lower()
    if any(x in low for x in ["503", "unavailable", "overloaded"]):
        return OVERLOADED_ERROR
    if has_retry_after_hint(text):
        return tag_error(ErrorCode.RATE_LIMIT, "⏱️ Временный лимит запросов. Подождите немного.")
    if "quota" in low or "limit" in low:
        return QUOTA_ERROR
    if "timeout" in low or "timed out" in low:
        return TIMEOUT_ERROR
    return GENERIC_ERROR


def is_error_message(text: str) -> bool:
    """Определяет, является ли сообщение ошибкой."""
    if not text:
        return False
    # Fast path: tagged error
    if extract_error_code(text) is not None:
        return True
    # Legacy fallback: emoji prefix check
    error_indicators = ["⏰", "❌", "🔄", "🚫", "⏱️", "💳", "🌐", "🔑"]
    return any(text.startswith(indicator) for indicator in error_indicators)


def is_retryable_error(text: str) -> bool:
    """Определяет, можно ли повторить запрос при этой ошибке."""
    if not text:
        return False
    # Fast path: tagged error
    code = extract_error_code(text)
    if code is not None:
        return _ERROR_PROPERTIES.get(code, (False, False, "transient"))[0]
    if has_retry_after_hint(text):
        return True
    # Legacy fallback: text pattern matching
    retryable_patterns = [
        "⏰",
        "🔄",
        "⏱️",
        "🌐",
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
    return any(pattern.lower() in text_lower or text.startswith(pattern) for pattern in retryable_patterns)


def is_key_related_error(text: str) -> bool:
    """Определяет, является ли ошибка связанной с ключом API."""
    if not text:
        return False
    # Fast path: tagged error
    code = extract_error_code(text)
    if code is not None:
        return _ERROR_PROPERTIES.get(code, (False, False, "transient"))[1]
    if has_retry_after_hint(text):
        return True
    # Legacy fallback: text pattern matching
    text_lower = text.lower()
    not_key_patterns = [
        "⏰",
        "🔄",
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
    if any(p.lower() in text_lower or text.startswith(p) for p in not_key_patterns):
        return False
    key_patterns = [
        "🚫",
        "⏱️",
        "🔑",
        "quota",
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
    return any(p.lower() in text_lower or text.startswith(p) for p in key_patterns)


def classify_key_error(text: str) -> str:
    """Classify error into penalty category: permanent/quota/rate_limit/transient."""
    if not text:
        return "transient"
    # Fast path: tagged error
    code = extract_error_code(text)
    if code is not None:
        return _ERROR_PROPERTIES.get(code, (False, False, "transient"))[2]
    if has_retry_after_hint(text):
        return "rate_limit"
    # Legacy fallback: text pattern matching
    text_lower = text.lower()
    transient = [
        "⏰",
        "🔄",
        "503",
        "unavailable",
        "overloaded",
        "timeout",
        "timed out",
        "превышено время ожидания",
        "перегружен",
        "некорректный запрос",
        "invalid request",
        "malformed",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in transient):
        return "transient"
    permanent = [
        "🔑",
        "api_key_invalid",
        "invalid api key",
        "authentication",
        "unauthorized",
        "forbidden",
        "неверный api ключ",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in permanent):
        return "permanent"
    quota = [
        "🚫",
        "quota",
        "quota exceeded",
        "daily limit",
        "limit exceeded",
        "достигнут лимит",
    ]
    if any(p.lower() in text_lower or text.startswith(p) for p in quota):
        return "quota"
    rate = ["⏱️", "rate limit", "rate_limit", "превышен лимит", "лимит запросов"]
    if any(p.lower() in text_lower or text.startswith(p) for p in rate):
        return "rate_limit"
    return "transient"


# =============================================================================
# KEYBOARD BUILDERS
# =============================================================================


def build_retry_and_roles_keyboard(include_roles: bool = True) -> InlineKeyboardMarkup:
    """Builds keyboard with retry button, edit query, and optional roles button."""
    buttons = [
        [
            InlineKeyboardButton("🔁 Повторить", callback_data="retry_last"),
            InlineKeyboardButton("✏️ Изменить запрос", callback_data="edit_query"),
        ],
    ]
    if include_roles:
        buttons.append([InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")])
    return InlineKeyboardMarkup(buttons)


def build_roles_keyboard() -> InlineKeyboardMarkup:
    """Builds keyboard with only roles button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles:from_response")]]
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
        self.raw_error = str(raw_error) if isinstance(raw_error, Exception) else raw_error
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

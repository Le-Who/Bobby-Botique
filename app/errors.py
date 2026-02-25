"""
Unified error handling for GemAI Bot v2.

This module provides:
- User-friendly error message constants
- Error classification functions (retryable, key-related)
- APIError exception class with auto-detection
- handle_api_errors async context manager for standardized handling
- Keyboard builders for error recovery UI
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional, Callable, Any, Union
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message


# =============================================================================
# USER-FRIENDLY ERROR MESSAGES (единая точка кастомfromации)
# =============================================================================
GENERIC_ERROR = "❌ Произошла ошибка. Попробуйте ещё раз."
OVERLOADED_ERROR = "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
QUOTA_ERROR = "🚫 Достигнут лимит запросов к API."
PROCESSING_ERROR = "❌ Ошибка обработки запроса."
DOCUMENT_ERROR = "❌ Ошибка обработки содержимого документа."
TIMEOUT_ERROR = "⏰ Превышено время ожидания. Попробуйте ещё раз."
NETWORK_ERROR = "🌐 Ошибка сети. Проверьте подключение."


# =============================================================================
# ERROR CLASSIFICATION FUNCTIONS (must be defined before APIError class)
# =============================================================================


def user_friendly_error(raw_error: Union[Exception, str]) -> str:
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
    error_indicators = ["⏰", "❌", "🔄", "🚫", "⏱️", "💳", "🌐"]
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
    Определяет, является ли ошибка связанной с keyом API.
    Такие ошибки требуют попытки с другим keyом.

    Ошибки, связанные с keyом:
    - Quota Exceeded (🚫) - key валиден, но исчерпан limit
    - Invalid API Key - key невалиден
    - Authentication Error - проблема с авторfromацией
    - Rate Limit (⏱️) - key валиден, но превышен limit requestов/сек

    Ошибки, НЕ связанные с keyом (не требуют смены keyа):
    - 503 Service Unavailable (🔄) - проблема сервера
    - Timeout (⏰) - проблема сети/сервера
    - Invalid Request (❌) - ошибка в requestе (коде)
    """
    if not text:
        return False

    text_lower = text.lower()

    # Ошибки, связанные с keyом - пробуем другой key
    key_related_patterns = [
        "🚫",  # Quota/лимит
        "⏱️",  # Rate limit
        "quota",
        "quota exceeded",
        "limit",
        "invalid api key",
        "authentication",
        "unauthorized",
        "forbidden",
        "api key",
        "api_key",
        "rate limit",
        "rate_limit",
        "достигнут лимит",
        "превышен лимит",
        "лимит запросов",
    ]

    # Ошибки, НЕ связанные с keyом - не меняем key
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

    # Сначала проверяем на ошибки, НЕ связанные с keyом (onоритет выше)
    if any(
        pattern.lower() in text_lower or text.startswith(pattern)
        for pattern in not_key_related_patterns
    ):
        return False

    # Check на ошибки, связанные с keyом
    return any(
        pattern.lower() in text_lower or text.startswith(pattern)
        for pattern in key_related_patterns
    )


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
# APIError EXCEPTION CLASS
# =============================================================================


class APIError(Exception):
    """
    Base exception for API-related errors with user-friendly messages.

    Attributes:
        raw_error: Original error text or exception
        retryable: Whether the error can be resolved by retrying
        key_related: Whether the error requires trying a different API key
        user_message: User-friendly error message

    Usage:
        raise APIError("Quota exceeded", retryable=False, key_related=True)

        # Or from existing exception:
        except SomeException as e:
            raise APIError.from_exception(e)
    """

    def __init__(
        self,
        raw_error: Union[str, Exception],
        retryable: bool = False,
        key_related: bool = False,
    ):
        self.raw_error = (
            str(raw_error) if isinstance(raw_error, Exception) else raw_error
        )
        self.retryable = retryable
        self.key_related = key_related
        self.user_message = user_friendly_error(self.raw_error)
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
    on_error: Optional[Callable[[Exception], Any]] = None,
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

    Usage:
        async with handle_api_errors(message, "Gemini request"):
            response = await get_ai_response(...)
            await message.edit_text(response)

    Args:
        placeholder_message: Telegram message to edit with error
        context: Description of the operation for logging
        on_error: Optional callback to run on error (sync or async)
        show_retry_button: Whether to show retry keyboard on error
        reraise: Whether to re-raise the exception after handling
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

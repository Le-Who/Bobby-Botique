import logging
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# Пользовательские сообщения об ошибках (единая точка кастомизации)
GENERIC_ERROR = "❌ Произошла ошибка. Попробуйте ещё раз."
OVERLOADED_ERROR = "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
QUOTA_ERROR = "🚫 Достигнут лимит запросов к API."
PROCESSING_ERROR = "❌ Ошибка обработки запроса."
DOCUMENT_ERROR = "❌ Ошибка обработки содержимого документа."


def user_friendly_error(raw_error: Exception | str) -> str:
    """Возвращает короткое дружелюбное сообщение для пользователя."""
    text = str(raw_error) if not isinstance(raw_error, Exception) else str(raw_error)
    low = (text or "").lower()
    if any(x in low for x in ["503", "unavailable", "overloaded"]):
        return OVERLOADED_ERROR
    if "quota" in low or "limit" in low:
        return QUOTA_ERROR
    return GENERIC_ERROR


def build_retry_and_roles_keyboard(include_roles: bool = True) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("🔁 Попробовать ещё раз", callback_data="retry_last")]]
    if include_roles:
        buttons.append([InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")])
    return InlineKeyboardMarkup(buttons)


def build_roles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")]])


def log_and_format_error(context: str, err: Exception) -> str:
    logging.error(f"{context}: {err}", exc_info=True)
    return user_friendly_error(err)


def is_error_message(text: str) -> bool:
    """Определяет, является ли сообщение ошибкой по наличию эмодзи ошибок."""
    if not text:
        return False
    error_indicators = ["⏰", "❌", "🔄", "🚫", "⏱️", "💳"]
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
        "Превышено время ожидания",
        "перегружен",
        "rate limit",
        "503",
        "unavailable",
        "overloaded"
    ]
    text_lower = text.lower()
    return any(pattern.lower() in text_lower or text.startswith(pattern) for pattern in retryable_patterns)


def is_key_related_error(text: str) -> bool:
    """
    Определяет, является ли ошибка связанной с ключом API.
    Такие ошибки требуют попытки с другим ключом.
    
    Ошибки, связанные с ключом:
    - Quota Exceeded (🚫) - ключ валиден, но исчерпан лимит
    - Invalid API Key - ключ невалиден
    - Authentication Error - проблема с авторизацией
    - Rate Limit (⏱️) - ключ валиден, но превышен лимит запросов/сек
    
    Ошибки, НЕ связанные с ключом (не требуют смены ключа):
    - 503 Service Unavailable (🔄) - проблема сервера
    - Timeout (⏰) - проблема сети/сервера
    - Invalid Request (❌) - ошибка в запросе (коде)
    """
    if not text:
        return False
    
    text_lower = text.lower()
    
    # Ошибки, связанные с ключом - пробуем другой ключ
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
        "лимит запросов"
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
        "malformed"
    ]
    
    # Сначала проверяем на ошибки, НЕ связанные с ключом (приоритет выше)
    if any(pattern.lower() in text_lower or text.startswith(pattern) for pattern in not_key_related_patterns):
        return False
    
    # Проверяем на ошибки, связанные с ключом
    return any(pattern.lower() in text_lower or text.startswith(pattern) for pattern in key_related_patterns)



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



from app.utils.text_format import (
    format_text as new_format_text,
)
from app.utils.text_format import (
    strip_formatting as new_strip_formatting,
)


def escape_format_chars(text: str) -> str:
    """Экранирует фигурные скобки { и } для безопасного форматирования строк Python"""
    if not text:
        return text
    return text.replace("{", "{{").replace("}", "}}")


def format_key_for_display(api_key: str) -> str:
    """Formats an API key for safe display."""
    if not isinstance(api_key, str) or len(api_key) < 10:
        return "Invalid Key"
    return f"{api_key[:5]}...{api_key[-4:]}"


def strip_markdown(text: str) -> str:
    """Removes all formatting from the text."""
    return new_strip_formatting(text)


class TelegramFormatter:
    """
    Класс for форматирования textа for Telegram.
    Now acts as a wrapper around the robust HTML formatter in app.utils.text_format.
    """

    @classmethod
    def format_text(cls, text: str, preserve_formatting: bool = True) -> tuple[str, str | None]:
        """
        Форматирует text for отправки в Telegram.

        Args:
            text: Исходный text
            preserve_formatting: Сохранять ли форматирование

        Returns:
            Tuple[str, str]: (отформатированный_text, parse_mode)
        """
        if not preserve_formatting or not text:
            return new_strip_formatting(text), None

        # Always use HTML as it is more robust
        return new_format_text(text, parse_mode="HTML")

import asyncio
import logging
from telegram import Message
from telegram.error import BadRequest
from .formatting import strip_markdown, escape_markdown_v2
from ..config import settings

async def send_long_message(message: Message, text: str):
    """
    Splits a long message and sends it in parts.
    It now uses a robust sanitizer before attempting to send as MarkdownV2.
    """
    parts = []
    while len(text) > 0:
        if len(text) <= settings.TELEGRAM_MESSAGE_LIMIT:
            parts.append(text)
            break
        part = text[:settings.TELEGRAM_MESSAGE_LIMIT]
        last_newline = part.rfind('\n')
        slice_index = last_newline if last_newline != -1 else settings.TELEGRAM_MESSAGE_LIMIT
        parts.append(text[:slice_index])
        text = text[slice_index + 1:] if last_newline != -1 else text[slice_index:]

    is_first_part = True
    for part in parts:
        if not part.strip():
            continue
        
        # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
        # 1. Применяем наш новый "умный" эскейпер.
        sanitized_part = escape_markdown_v2(part)
        
        try:
            # 2. Пытаемся отправить обработанный текст.
            if is_first_part:
                await message.edit_text(sanitized_part, parse_mode='MarkdownV2')
            else:
                await message.reply_text(sanitized_part, parse_mode='MarkdownV2')
        except BadRequest:
            # 3. Если ДАЖЕ ПОСЛЕ ЭТОГО происходит ошибка, падаем в plain text.
            # Это наша финальная линия защиты.
            logging.warning(f"MarkdownV2 parsing failed even after sanitizing. Falling back to plain text.")
            cleaned_part = strip_markdown(part) # Используем оригинальный, не "заэскейпленный" кусок
            try:
                if is_first_part:
                    await message.edit_text(cleaned_part)
                else:
                    await message.reply_text(cleaned_part)
            except Exception as e:
                logging.error(f"Failed to send final fallback message: {e}")

        is_first_part = False
        await asyncio.sleep(0.3)

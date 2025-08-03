import asyncio
import logging
from telegram import Message
from telegram.error import BadRequest
from .formatting import strip_markdown
from ..config import settings

async def send_long_message(message: Message, text: str):
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
        
        try:
            if is_first_part:
                await message.edit_text(part, parse_mode='MarkdownV2')
            else:
                await message.reply_text(part, parse_mode='MarkdownV2')
        except BadRequest:
            logging.warning(f"MarkdownV2 parsing failed for a part, falling back to clean plain text.")
            cleaned_part = strip_markdown(part)
            if is_first_part:
                await message.edit_text(cleaned_part)
            else:
                await message.reply_text(cleaned_part)
        
        is_first_part = False
        await asyncio.sleep(0.3)

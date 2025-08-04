import asyncio
import logging
from telegram import Message
from telegram.error import BadRequest
from .formatting import strip_markdown, escape_markdown_v2, TelegramFormatter
from ..config import settings

async def send_long_message(message: Message, text: str, preserve_formatting: bool = True):
    """
    Splits a long message and sends it in parts using the new TelegramFormatter.
    
    Args:
        message: Telegram message object
        text: Text to send
        preserve_formatting: Whether to preserve formatting (default: True)
    """
    if not text or not text.strip():
        return
    
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
        
        # Используем новую систему форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(part, preserve_formatting)
        
        try:
            if is_first_part:
                if parse_mode:
                    await message.edit_text(formatted_text, parse_mode=parse_mode)
                else:
                    await message.edit_text(formatted_text)
            else:
                if parse_mode:
                    await message.reply_text(formatted_text, parse_mode=parse_mode)
                else:
                    await message.reply_text(formatted_text)
                    
        except BadRequest as e:
            # Если форматирование не удалось, пробуем без форматирования
            logging.warning(f"Formatting failed for part {len(parts)}: {e}. Falling back to plain text.")
            try:
                plain_text = TelegramFormatter._strip_all_formatting(part)
                if is_first_part:
                    await message.edit_text(plain_text)
                else:
                    await message.reply_text(plain_text)
            except Exception as fallback_error:
                logging.error(f"Failed to send fallback message: {fallback_error}")
                # Последняя попытка - отправить как есть
                try:
                    if is_first_part:
                        await message.edit_text(part)
                    else:
                        await message.reply_text(part)
                except Exception as final_error:
                    logging.error(f"Final attempt to send message failed: {final_error}")

        except Exception as e:
            logging.error(f"Unexpected error sending message part: {e}")
            # Пытаемся отправить как есть
            try:
                if is_first_part:
                    await message.edit_text(part)
                else:
                    await message.reply_text(part)
            except Exception as final_error:
                logging.error(f"Failed to send message even as plain text: {final_error}")

        is_first_part = False
        await asyncio.sleep(0.3)

async def send_formatted_message(message: Message, text: str, parse_mode: str = None):
    """
    Отправляет отформатированное сообщение с указанным parse_mode.
    
    Args:
        message: Telegram message object
        text: Text to send
        parse_mode: Parse mode ('MarkdownV2', 'HTML', or None)
    """
    try:
        await message.reply_text(text, parse_mode=parse_mode)
    except BadRequest as e:
        logging.warning(f"Failed to send formatted message with {parse_mode}: {e}")
        # Fallback to plain text
        try:
            plain_text = TelegramFormatter._strip_all_formatting(text)
            await message.reply_text(plain_text)
        except Exception as fallback_error:
            logging.error(f"Failed to send fallback message: {fallback_error}")

async def edit_formatted_message(message: Message, text: str, parse_mode: str = None):
    """
    Редактирует сообщение с форматированием.
    
    Args:
        message: Telegram message object
        text: New text
        parse_mode: Parse mode ('MarkdownV2', 'HTML', or None)
    """
    try:
        await message.edit_text(text, parse_mode=parse_mode)
    except BadRequest as e:
        logging.warning(f"Failed to edit formatted message with {parse_mode}: {e}")
        # Fallback to plain text
        try:
            plain_text = TelegramFormatter._strip_all_formatting(text)
            await message.edit_text(plain_text)
        except Exception as fallback_error:
            logging.error(f"Failed to edit fallback message: {fallback_error}")

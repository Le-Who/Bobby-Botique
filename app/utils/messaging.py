import asyncio
import logging
from telegram import Message
from telegram.error import BadRequest
from .formatting import strip_markdown, escape_markdown_v2, TelegramFormatter
from ..config import settings

async def send_long_message(message: Message, text: str, preserve_formatting: bool = True, reply_markup=None, from_cache: bool = False, cache_key: str = None):
    """
    Splits a long message and sends it in parts using the new TelegramFormatter.
    
    Args:
        message: Telegram message object
        text: Text to send
        preserve_formatting: Whether to preserve formatting (default: True)
        reply_markup: Reply markup for the message
        from_cache: Whether this response is from cache
        cache_key: Cache key for refreshing the response
    """
    if not text or not text.strip():
        return
    
    # Добавляем индикацию кэша, если ответ из кэша
    if from_cache:
        cache_indicator = "\n\n💾 *Ответ получен из кэша*\n"
        if cache_key:
            # Создаем кнопку для актуализации ответа
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            if reply_markup:
                # Добавляем кнопку к существующей разметке
                if hasattr(reply_markup, 'inline_keyboard'):
                    new_keyboard = reply_markup.inline_keyboard + [[InlineKeyboardButton("🔄 Актуализировать ответ", callback_data=f"refresh:{cache_key}")]]
                    reply_markup = InlineKeyboardMarkup(new_keyboard)
                else:
                    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Актуализировать ответ", callback_data=f"refresh:{cache_key}")]])
            else:
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Актуализировать ответ", callback_data=f"refresh:{cache_key}")]])
        text = cache_indicator + text
    
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
    current_message = message  # Текущее сообщение для редактирования
    
    for part in parts:
        if not part.strip():
            continue
        
        # Используем новую систему форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(part, preserve_formatting)
        
        try:
            if is_first_part:
                if parse_mode:
                    await current_message.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
                else:
                    await current_message.edit_text(formatted_text, reply_markup=reply_markup)
            else:
                if parse_mode:
                    current_message = await current_message.reply_text(formatted_text, parse_mode=parse_mode)
                else:
                    current_message = await current_message.reply_text(formatted_text)
                    
        except BadRequest as e:
            # Если редактирование не удалось, создаем новое сообщение
            if "Message can't be edited" in str(e) and is_first_part:
                logging.warning(f"Message can't be edited, creating new message: {e}")
                try:
                    if parse_mode:
                        current_message = await message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
                    else:
                        current_message = await message.reply_text(formatted_text, reply_markup=reply_markup)
                except Exception as new_msg_error:
                    logging.error(f"Failed to create new message: {new_msg_error}")
                    # Последняя попытка - отправить как есть
                    try:
                        current_message = await message.reply_text(part, reply_markup=reply_markup if is_first_part else None)
                    except Exception as final_error:
                        logging.error(f"Final attempt to send message failed: {final_error}")
                        return
            else:
                # Если форматирование не удалось, пробуем без форматирования
                logging.warning(f"Formatting failed for part {len(parts)}: {e}. Falling back to plain text.")
                try:
                    plain_text = TelegramFormatter._strip_all_formatting(part)
                    if is_first_part:
                        await current_message.edit_text(plain_text, reply_markup=reply_markup)
                    else:
                        current_message = await current_message.reply_text(plain_text)
                except Exception as fallback_error:
                    logging.error(f"Failed to send fallback message: {fallback_error}")
                    # Последняя попытка - отправить как есть
                    try:
                        if is_first_part:
                            current_message = await message.reply_text(part, reply_markup=reply_markup)
                        else:
                            current_message = await message.reply_text(part)
                    except Exception as final_error:
                        logging.error(f"Final attempt to send message failed: {final_error}")

        except Exception as e:
            logging.error(f"Unexpected error sending message part: {e}")
            # Пытаемся отправить как есть
            try:
                if is_first_part:
                    current_message = await message.reply_text(part, reply_markup=reply_markup)
                else:
                    current_message = await current_message.reply_text(part)
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

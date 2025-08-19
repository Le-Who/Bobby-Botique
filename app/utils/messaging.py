import asyncio
import logging
from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from .formatting import strip_markdown, escape_markdown_v2, TelegramFormatter
from ..config import settings

async def send_long_message(message: Message, text: str, preserve_formatting: bool = True, reply_markup=None, is_deep_dive: bool = False):
    """
    Splits a long message and sends it in parts using the new TelegramFormatter.
    
    Args:
        message: Telegram message object
        text: Text to send
        preserve_formatting: Whether to preserve formatting (default: True)
        reply_markup: The reply markup to use for the message.
        is_deep_dive: Flag to indicate if the message is part of a deep dive session.
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
    current_message = message  # Текущее сообщение для редактирования
    
    for i, part in enumerate(parts):
        if not part.strip():
            continue

        # Determine the keyboard for this part
        current_reply_markup = None
        is_last_part = (i == len(parts) - 1)

        if is_deep_dive:
            if is_last_part:
                keyboard = [
                    [InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")],
                    [InlineKeyboardButton("👇 Копнуть глубже", callback_data="deepdive:deeper_dive")]
                ]
                current_reply_markup = InlineKeyboardMarkup(keyboard)
            else:
                keyboard = [[InlineKeyboardButton("✨ Начать новую тему", callback_data="deepdive:new_topic")]]
                current_reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            keyboard = [[InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")]]
            current_reply_markup = InlineKeyboardMarkup(keyboard)

        # Используем новую систему форматирования
        formatted_text, parse_mode = TelegramFormatter.format_text(part, preserve_formatting)
        
        try:
            if is_first_part:
                if parse_mode:
                    await current_message.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=current_reply_markup)
                else:
                    await current_message.edit_text(formatted_text, reply_markup=current_reply_markup)
            else:
                if parse_mode:
                    current_message = await current_message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=current_reply_markup)
                else:
                    current_message = await current_message.reply_text(formatted_text, reply_markup=current_reply_markup)
                    
        except BadRequest as e:
            # Если редактирование не удалось, создаем новое сообщение
            if "Message can't be edited" in str(e) and is_first_part:
                logging.warning(f"Message can't be edited, creating new message: {e}")
                try:
                    if parse_mode:
                        current_message = await message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=current_reply_markup)
                    else:
                        current_message = await message.reply_text(formatted_text, reply_markup=current_reply_markup)
                except Exception as new_msg_error:
                    logging.error(f"Failed to create new message: {new_msg_error}")
                    # Последняя попытка - отправить как есть
                    try:
                        current_message = await message.reply_text(part, reply_markup=current_reply_markup if is_first_part else None)
                    except Exception as final_error:
                        logging.error(f"Final attempt to send message failed: {final_error}")
                        return
            else:
                # Если форматирование не удалось, пробуем без форматирования
                logging.warning(f"Formatting failed for part {len(parts)}: {e}. Falling back to plain text.")
                try:
                    plain_text = TelegramFormatter._strip_all_formatting(part)
                    if is_first_part:
                        await current_message.edit_text(plain_text, reply_markup=current_reply_markup)
                    else:
                        current_message = await current_message.reply_text(plain_text, reply_markup=current_reply_markup)
                except Exception as fallback_error:
                    logging.error(f"Failed to send fallback message: {fallback_error}")
                    # Последняя попытка - отправить как есть
                    try:
                        if is_first_part:
                            current_message = await message.reply_text(part, reply_markup=current_reply_markup)
                        else:
                            current_message = await current_message.reply_text(part, reply_markup=current_reply_markup)
                    except Exception as final_error:
                        logging.error(f"Final attempt to send message failed: {final_error}")

        except Exception as e:
            logging.error(f"Unexpected error sending message part: {e}")
            # Пытаемся отправить как есть
            try:
                if is_first_part:
                    current_message = await message.reply_text(part, reply_markup=current_reply_markup)
                else:
                    current_message = await current_message.reply_text(part, reply_markup=current_reply_markup)
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

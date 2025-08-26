import asyncio
import logging
import re
from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from app.utils.formatting import strip_markdown, escape_markdown_v2, TelegramFormatter

def _strip_formatting(text: str) -> str:
    """Удаляет все форматирование из текста."""
    # Удаляем Markdown
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'[*_~`]', '', text)
    text = text.replace('\\', '')
    
    # Удаляем HTML теги
    text = re.sub(r'<[^>]*>', '', text)
    
    return text.strip()

def _split_text(text: str, max_length: int = 4000) -> list:
    """
    Разбивает длинный текст на части, не превышающие максимальную длину.
    
    Args:
        text: Text to split
        max_length: Maximum length of each part
        
    Returns:
        List of text parts
    """
    if not text or len(text) <= max_length:
        return [text] if text else []
    
    parts = []
    while text and len(text) > 0:
        if len(text) <= max_length:
            parts.append(text)
            break
        
        # Ищем последний перенос строки в пределах лимита
        part = text[:max_length]
        last_newline = part.rfind('\n')
        
        # Если нашли перенос строки, используем его как границу
        if last_newline != -1:
            slice_index = last_newline
        else:
            # Иначе просто обрезаем по лимиту
            slice_index = max_length
        
        parts.append(text[:slice_index])
        text = text[slice_index + 1:] if last_newline != -1 else text[slice_index:]
        
        # Дополнительная проверка безопасности
        if not text or len(text) == 0:
            break
    
    return parts
from app.config import settings

async def send_long_message(message: Message, text: str, is_deep_dive: bool = False, reply_markup=None, preserve_formatting: bool = False):
    """
    Отправляет длинное сообщение, разбивая его на части, если необходимо.
    
    Args:
        message: Telegram message object
        text: Text to send
        is_deep_dive: Whether this is a deep dive response
        reply_markup: Inline keyboard markup
        preserve_formatting: Whether to preserve original formatting
    """
    # Валидация состояния deep dive
    if is_deep_dive:
        try:
            from app.database import get_user_chat
            user_id = message.from_user.id if message.from_user else None
            if user_id:
                chat_state = await get_user_chat(user_id)
                if not chat_state.is_deep_dive:
                    logging.warning(f"Deep dive flag set but user {user_id} not in deep dive mode")
                    is_deep_dive = False  # Сбрасываем флаг для безопасности
                elif not chat_state.deep_dive_thread_id:
                    logging.warning(f"Deep dive flag set but no thread_id for user {user_id}")
                    is_deep_dive = False  # Сбрасываем флаг для безопасности
                else:
                    # Дополнительная проверка: убеждаемся, что deep dive действительно активен
                    logging.info(f"Deep dive mode confirmed for user {user_id}")
        except Exception as e:
            logging.error(f"Error validating deep dive state: {e}")
            is_deep_dive = False  # Сбрасываем флаг для безопасности
    
    # Разбиваем текст на части, если он слишком длинный
    parts = _split_text(text, max_length=4000)

    is_first_part = True
    current_message = message  # Текущее сообщение для редактирования
    
    for i, part in enumerate(parts or []):
        if not part or not part.strip():
            continue

        # Determine the keyboard for this part
        current_reply_markup = None
        is_last_part = (i == len(parts) - 1) if parts and len(parts) > 0 else True

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
                logging.warning(f"Formatting failed for part {i+1}/{len(parts)}: {e}. Falling back to plain text.")
                try:
                    plain_text = _strip_formatting(part)
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
                            current_message = await current_message.reply_text(plain_text, reply_markup=current_reply_markup)
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
            plain_text = _strip_formatting(text)
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
            plain_text = _strip_formatting(text)
            await message.edit_text(plain_text)
        except Exception as fallback_error:
            logging.error(f"Failed to edit fallback message: {fallback_error}")

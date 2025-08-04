import asyncio
import logging
from telegram import Message
from telegram.error import BadRequest
from .formatting import strip_markdown, sanitize_for_telegram
from ..config import settings

async def send_simple_message(message: Message, text: str):
    """
    Sends a simple message without any formatting.
    
    Args:
        message: Telegram message object
        text: Text to send
    """
    if not text or not text.strip():
        return
    
    try:
        await message.edit_text(text)
    except Exception as e:
        logging.error(f"Failed to send simple message: {e}")

async def send_long_message(message: Message, text: str, prefer_plain: bool = False):
    """
    Splits a long message and sends it in parts with reliable formatting.
    
    Args:
        message: Telegram message object
        text: Text to send
        prefer_plain: If True, prefer plain text over MarkdownV2
    """
    if not text or not text.strip():
        return
    
    # Split text into parts
    parts = []
    remaining_text = text
    
    while len(remaining_text) > 0:
        if len(remaining_text) <= settings.TELEGRAM_MESSAGE_LIMIT:
            parts.append(remaining_text)
            break
        
        # Find a good break point
        part = remaining_text[:settings.TELEGRAM_MESSAGE_LIMIT]
        last_newline = part.rfind('\n')
        last_period = part.rfind('. ')
        last_space = part.rfind(' ')
        
        # Prefer newline, then period, then space
        break_point = last_newline if last_newline != -1 else (
            last_period + 1 if last_period != -1 else (
                last_space if last_space != -1 else settings.TELEGRAM_MESSAGE_LIMIT
            )
        )
        
        parts.append(remaining_text[:break_point])
        remaining_text = remaining_text[break_point:].lstrip()
    
    # Send each part
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        
        # Sanitize the text
        # If text contains links, prefer MarkdownV2 to preserve them
        contains_links = '[' in part and '](' in part and ')' in part
        sanitized_text, is_markdown = sanitize_for_telegram(part, prefer_plain and not contains_links)
        
        try:
            if i == 0:
                # First part - edit the original message
                if is_markdown:
                    await message.edit_text(sanitized_text, parse_mode='MarkdownV2')
                else:
                    await message.edit_text(sanitized_text)
            else:
                # Subsequent parts - send new messages
                if is_markdown:
                    await message.reply_text(sanitized_text, parse_mode='MarkdownV2')
                else:
                    await message.reply_text(sanitized_text)
                    
        except BadRequest as e:
            # If MarkdownV2 fails, fall back to plain text
            if is_markdown:
                logging.warning(f"MarkdownV2 failed, falling back to plain text: {e}")
                plain_text = strip_markdown(part)
                try:
                    if i == 0:
                        await message.edit_text(plain_text)
                    else:
                        await message.reply_text(plain_text)
                except Exception as fallback_error:
                    logging.error(f"Failed to send fallback message: {fallback_error}")
            else:
                logging.error(f"Failed to send message: {e}")
        except Exception as e:
            logging.error(f"Unexpected error sending message: {e}")
        
        # Small delay between messages
        if i < len(parts) - 1:
            await asyncio.sleep(0.3)

async def send_formatted_message(message: Message, text: str, bold_parts: list = None, italic_parts: list = None):
    """
    Sends a message with simple formatting (bold and italic).
    
    Args:
        message: Telegram message object
        text: Base text
        bold_parts: List of strings to make bold
        italic_parts: List of strings to make italic
    """
    from .formatting import create_simple_formatted_text, strip_markdown
    
    if not text:
        return
    
    # If no formatting is requested, send as plain text
    if not bold_parts and not italic_parts:
        try:
            await message.edit_text(text)
        except Exception as e:
            logging.error(f"Failed to send plain message: {e}")
        return
    
    # Create formatted text only for the parts that need formatting
    formatted_text = create_simple_formatted_text(text, bold_parts, italic_parts)
    
    try:
        await message.edit_text(formatted_text, parse_mode='MarkdownV2')
    except BadRequest:
        # Fall back to plain text - completely strip all formatting
        logging.warning("Formatted text failed, falling back to plain text")
        plain_text = strip_markdown(text)
        try:
            await message.edit_text(plain_text)
        except Exception as fallback_error:
            logging.error(f"Failed to send fallback plain text: {fallback_error}")
    except Exception as e:
        logging.error(f"Failed to send formatted message: {e}")

async def send_list_message(message: Message, title: str, items: list, prefix: str = "•"):
    """
    Sends a formatted list message.
    
    Args:
        message: Telegram message object
        title: Title for the list
        items: List of items
        prefix: Prefix for each item
    """
    from .formatting import format_list, create_simple_formatted_text, strip_markdown
    
    if not items:
        await message.edit_text(f"{title}\n\nСписок пуст.")
        return
    
    # Format the list
    formatted_list = format_list(items, prefix)
    full_text = f"{title}\n\n{formatted_list}"
    
    try:
        # Try with simple formatting
        formatted_text = create_simple_formatted_text(full_text, bold_parts=[title])
        await message.edit_text(formatted_text, parse_mode='MarkdownV2')
    except BadRequest:
        # Fall back to plain text
        logging.warning("List formatting failed, falling back to plain text")
        plain_text = strip_markdown(full_text)
        try:
            await message.edit_text(plain_text)
        except Exception as fallback_error:
            logging.error(f"Failed to send fallback list message: {fallback_error}")
    except Exception as e:
        logging.error(f"Failed to send list message: {e}")

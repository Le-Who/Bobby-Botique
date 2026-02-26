import asyncio
import logging
from telegram import Message, InlineKeyboardMarkup
from telegram.error import BadRequest
from app.utils.text_format import format_text, split_text_safe, strip_formatting
from app.utils.keyboards import ai_response_keyboard, deep_dive_keyboard


async def send_long_message(
    message: Message,
    text: str,
    is_deep_dive: bool = False,
    reply_markup=None,
    preserve_formatting: bool = True,
):
    """
    Отправляет длинное message, разбивая его на части, if необходимо.
    Использует safe HTML форматирование.
    """
    # Validation состояния deep dive (legacy logic preserved)
    if is_deep_dive:
        try:
            from app.repos.chats import get_user_chat

            user_id = message.from_user.id if message.from_user else None
            if user_id:
                chat_state = await get_user_chat(user_id)
                if not chat_state.is_deep_dive:
                    logging.warning(
                        f"Deep dive flag set but user {user_id} not in deep dive mode"
                    )
                    is_deep_dive = False
                elif (
                    not hasattr(chat_state, "deep_dive_thread_id")
                    or not chat_state.deep_dive_thread_id
                ):
                    is_deep_dive = False
        except Exception as e:
            logging.error("Error validating deep dive state: %s", e)
            is_deep_dive = False

    # Format text в HTML
    formatted_text, parse_mode = format_text(text, parse_mode="HTML")

    # Разбиваем уже отформатированный text, сохраняя теги
    parts = split_text_safe(formatted_text)

    is_first_part = True
    current_message = message

    for i, part in enumerate(parts or []):
        if not part or not part.strip():
            continue

        # Determine the keyboard
        is_last_part = i == len(parts) - 1
        current_reply_markup = None

        if reply_markup is not None:
            current_reply_markup = reply_markup if is_last_part else None
        elif is_deep_dive:
            current_reply_markup = deep_dive_keyboard(is_last_part)
        else:
            current_reply_markup = ai_response_keyboard()

        # Sending logic
        try:
            if is_first_part:
                # Try to edit first
                await current_message.edit_text(
                    part,
                    parse_mode=parse_mode,
                    reply_markup=current_reply_markup,
                    disable_web_page_preview=True,
                )
            else:
                # Reply for subsequent parts
                current_message = await current_message.reply_text(
                    part,
                    parse_mode=parse_mode,
                    reply_markup=current_reply_markup,
                    disable_web_page_preview=True,
                )

        except BadRequest as e:
            logging.warning(
                f"Failed to send/edit message (parse_mode={parse_mode}): {e}"
            )

            # Retry without formatting if HTML fails (should be rare with our validator)
            try:
                plain_text = strip_formatting(part)
                if is_first_part:
                    try:
                        await current_message.edit_text(
                            plain_text, reply_markup=current_reply_markup
                        )
                    except BadRequest:
                        # If edit fails (e.g. content same), try sending new
                        current_message = await current_message.reply_text(
                            plain_text, reply_markup=current_reply_markup
                        )
                else:
                    current_message = await current_message.reply_text(
                        plain_text, reply_markup=current_reply_markup
                    )
            except Exception as final_error:
                logging.error("Critical error sending message: %s", final_error)

        except Exception as e:
            logging.error("Unexpected error in send_long_message: %s", e)

        is_first_part = False
        await asyncio.sleep(0.3)


async def send_formatted_message(message: Message, text: str, parse_mode: str = "HTML"):
    """Wrapper for sending formatted messages."""
    try:
        formatted, mode = format_text(text, parse_mode=parse_mode)
        await message.reply_text(formatted, parse_mode=mode)
    except Exception as e:
        logging.error("Error sending formatted message: %s", e)
        await message.reply_text(strip_formatting(text))


async def edit_formatted_message(message: Message, text: str, parse_mode: str = "HTML"):
    """Wrapper for editing formatted messages."""
    try:
        formatted, mode = format_text(text, parse_mode=parse_mode)
        await message.edit_text(formatted, parse_mode=mode)
    except Exception as e:
        logging.error("Error editing formatted message: %s", e)
        await message.edit_text(strip_formatting(text))

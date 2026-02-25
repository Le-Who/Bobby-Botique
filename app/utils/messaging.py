import asyncio
import logging
from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from app.utils.text_format import format_text, split_text_safe, strip_formatting


def _get_deep_dive_keyboard(is_last_part: bool) -> InlineKeyboardMarkup:
    """Get keyboard for deep dive mode responses."""
    buttons = [
        [
            InlineKeyboardButton("👍", callback_data="feedback:up"),
            InlineKeyboardButton("👎", callback_data="feedback:down"),
            InlineKeyboardButton("🔁", callback_data="retry_last"),
        ],
        [
            InlineKeyboardButton(
                "✨ Начать новую тему", callback_data="deepdive:new_topic"
            )
        ],
    ]
    if is_last_part:
        buttons.append(
            [
                InlineKeyboardButton(
                    "👇 Копнуть глубже", callback_data="deepdive:deeper_dive"
                )
            ]
        )
    buttons.append(
        [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")]
    )
    return InlineKeyboardMarkup(buttons)


def _get_default_response_keyboard() -> InlineKeyboardMarkup:
    """Get default keyboard for AI responses."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍", callback_data="feedback:up"),
                InlineKeyboardButton("👎", callback_data="feedback:down"),
                InlineKeyboardButton("🔁", callback_data="retry_last"),
            ],
            [InlineKeyboardButton("🎭 Выбрать роль ИИ", callback_data="open_roles")],
            [InlineKeyboardButton("✨ Начать новую тему", callback_data="new_topic")],
        ]
    )


async def send_long_message(
    message: Message,
    text: str,
    is_deep_dive: bool = False,
    reply_markup=None,
    preserve_formatting: bool = True,
):
    """
    Отправляет длинное сообщение, разбивая его на части, если необходимо.
    Использует безопасное HTML форматирование.
    """
    # Валидация состояния deep dive (legacy logic preserved)
    if is_deep_dive:
        try:
            from app.database import get_user_chat

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
            logging.error(f"Error validating deep dive state: {e}")
            is_deep_dive = False

    # Форматируем текст в HTML
    formatted_text, parse_mode = format_text(text, parse_mode="HTML")

    # Разбиваем уже отформатированный текст, сохраняя теги
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
            current_reply_markup = _get_deep_dive_keyboard(is_last_part)
        else:
            current_reply_markup = _get_default_response_keyboard()

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
                logging.error(f"Critical error sending message: {final_error}")

        except Exception as e:
            logging.error(f"Unexpected error in send_long_message: {e}")

        is_first_part = False
        await asyncio.sleep(0.3)


async def send_formatted_message(message: Message, text: str, parse_mode: str = "HTML"):
    """Wrapper for sending formatted messages."""
    try:
        formatted, mode = format_text(text, parse_mode=parse_mode)
        await message.reply_text(formatted, parse_mode=mode)
    except Exception as e:
        logging.error(f"Error sending formatted message: {e}")
        await message.reply_text(strip_formatting(text))


async def edit_formatted_message(message: Message, text: str, parse_mode: str = "HTML"):
    """Wrapper for editing formatted messages."""
    try:
        formatted, mode = format_text(text, parse_mode=parse_mode)
        await message.edit_text(formatted, parse_mode=mode)
    except Exception as e:
        logging.error(f"Error editing formatted message: {e}")
        await message.edit_text(strip_formatting(text))

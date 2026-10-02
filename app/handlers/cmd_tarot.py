"""
Command to initiate a Tarot session.
"""

import logging
import time

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers.tarot_chat import TAROT_END_SESSION_TEXT
from app.i18n import detect_language, t
from app.state import ensure_state_loaded, set_tarot_mode, set_tarot_session
from app.tarot import SpreadType, draw_cards
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter


@authorized_only
@safe_handler("Произошла ошибка при запуске режима Таро")
async def tarot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, question_context: str | None = None
) -> bool | None:
    user_id = update.effective_user.id
    await ensure_state_loaded(user_id)

    from app.handlers.compatibility import clear_compatibility_input

    # Prepare the session without changing the hydrated state.
    spread = SpreadType.CLASSIC

    from typing import Any

    session_data: dict[str, Any] = {
        "spread_type": spread.value,
        "drawn_cards": [],
        "history": [],
        "waiting_for_question": True,
        "last_activity_at": time.time(),
    }
    if question_context:
        session_data["question_context"] = question_context

    # Prepare UI (Reply Keyboard)
    keyboard = [[TAROT_END_SESSION_TEXT]]
    if question_context:
        keyboard.insert(0, [t("compat.draw_button", detect_language(question_context))])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # Format welcome message
    text = (
        "🔮 **Сеанс Таро начат**\n\n"
        "Пожалуйста, сосредоточьтесь на вашей ситуации и **задайте свой вопрос**.\n"
        "Как только вы напишете вопрос, я вытяну карты для расклада."
    )
    if question_context:
        text = t("compat.tarot_welcome", detect_language(question_context))

    formatted_text, parse_mode = TelegramFormatter.format_text(text)

    await context.bot.send_message(
        chat_id=user_id, text=formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
    )

    # Apply the transition only after delivery, without an intervening await.
    clear_compatibility_input(context.user_data)
    set_tarot_mode(user_id, True)
    set_tarot_session(user_id, session_data)
    return True

"""
Command to initiate a Tarot session.
"""

import time

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.handlers.tarot_chat import TAROT_END_SESSION_TEXT
from app.i18n import detect_language, t
from app.state import _schedule_persist, begin_interaction, ensure_state_loaded, run_interaction
from app.tarot import SpreadType
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter


@authorized_only
@safe_handler("Произошла ошибка при запуске режима Таро")
async def tarot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, question_context: str | None = None
) -> bool | None:
    user_id = update.effective_user.id
    await ensure_state_loaded(user_id)
    from app.handlers.interaction_work import reject_busy_interaction

    message = update.message or update.callback_query.message
    if await reject_busy_interaction(user_id, message.reply_text):
        return None

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

    owner = begin_interaction(user_id, "tarot")
    entered = False

    async def enter() -> None:
        nonlocal entered
        await context.bot.send_message(
            chat_id=user_id, text=formatted_text, parse_mode=parse_mode, reply_markup=reply_markup
        )
        if not owner.current():
            return
        from app.handlers.interaction_work import retire_context_text_input

        retire_context_text_input(context.user_data)
        # Publish only after delivery. A later command/cancel can fence this
        # welcome while preserving the previously saved session on failure.
        clear_compatibility_input(context.user_data)
        owner.state.tarot_mode = True
        owner.state.tarot_session = session_data
        _schedule_persist(owner.state)
        entered = True

    await run_interaction(owner, enter)
    return True if entered else None

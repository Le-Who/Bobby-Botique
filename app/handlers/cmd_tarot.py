"""
Command to initiate a Tarot session.
"""

import logging

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.state import set_tarot_mode, set_tarot_session
from app.tarot import SpreadType, draw_cards
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter


@authorized_only
@safe_handler("Произошла ошибка при запуске режима Таро")
async def tarot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # 1. Enable tarot mode
    set_tarot_mode(user_id, True)
    
    # 2. Setup session without drawing cards yet
    spread = SpreadType.CLASSIC
    
    from typing import Any
    # 3. Create session
    session_data: dict[str, Any] = {
        "spread_type": spread.value,
        "drawn_cards": [],
        "history": [],
        "waiting_for_question": True
    }
    set_tarot_session(user_id, session_data)
    
    # 4. Show UI (Reply Keyboard)
    keyboard = [
        ["🛑 Завершить сеанс Таро"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # 5. Format welcome message
    text = (
        "🔮 **Сеанс Таро начат**\n\n"
        "Пожалуйста, сосредоточьтесь на вашей ситуации и **задайте свой вопрос**.\n"
        "Как только вы напишете вопрос, я вытяну карты для расклада."
    )
    
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    
    await context.bot.send_message(
        chat_id=user_id,
        text=formatted_text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

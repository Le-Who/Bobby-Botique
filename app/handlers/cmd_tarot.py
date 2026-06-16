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
    
    # 2. Draw initial cards (3 card spread default)
    spread = SpreadType.CLASSIC
    cards = draw_cards(3)
    
    # 3. Create session
    session_data = {
        "spread_type": spread.value,
        "drawn_cards": cards,
        "history": []
    }
    set_tarot_session(user_id, session_data)
    
    # 4. Show UI (Reply Keyboard)
    keyboard = [
        ["🃏 Достать 1 карту", "🔄 Новый расклад"],
        ["🛑 Завершить сеанс Таро"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # 5. Format welcome message
    card_names = ", ".join([c["name"] for c in cards])
    text = (
        f"🔮 **Сеанс Таро начат**\n\n"
        f"Расклад (Классический - 3 карты):\n"
        f"**{card_names}**\n\n"
        "Задайте мне свой вопрос по этому раскладу."
    )
    
    formatted_text, parse_mode = TelegramFormatter.format_text(text)
    
    await update.message.reply_text(
        formatted_text,
        parse_mode=parse_mode,
        reply_markup=reply_markup
    )

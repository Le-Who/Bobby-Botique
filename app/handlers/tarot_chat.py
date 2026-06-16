"""
Tarot interactive mode chat handler.
Routes messages to Gemini using Tarot session context.
"""

import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes
from telegram.ext.filters import MessageFilter

from app.handlers.inline import _build_tarot_system_prompt
from app.providers.router import get_provider_router
from app.state import clear_tarot_session, get_tarot_session, is_in_tarot_mode, set_tarot_session
from app.tarot import SpreadType, draw_cards
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter


class IsTarotMode(MessageFilter):
    def filter(self, message) -> bool:
        if not message or not message.from_user:
            return False
        return is_in_tarot_mode(message.from_user.id)

is_tarot_mode_filter = IsTarotMode()

@authorized_only
@safe_handler("Произошла ошибка при обработке таро-запроса")
async def handle_tarot_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if text == "🛑 Завершить сеанс Таро":
        clear_tarot_session(user_id)
        await update.message.reply_text(
            "Сеанс Таро завершен. Возвращаюсь в обычный режим.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
        
    session = get_tarot_session(user_id)
    if session is None:
        clear_tarot_session(user_id)
        await update.message.reply_text("Сессия не найдена. Начните заново с /tarot", reply_markup=ReplyKeyboardRemove())
        return

    history = session.get("history", [])

    keyboard = [
        ["🃏 Достать 1 карту", "🔄 Новый расклад"],
        ["🛑 Завершить сеанс Таро"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    if text == "🔄 Новый расклад":
        spread = SpreadType.CLASSIC
        cards = draw_cards(3)
        
        session["spread_type"] = spread.value
        session["drawn_cards"] = cards
        session["history"] = []
        set_tarot_session(user_id, session)
        
        card_names = ", ".join([c["name"] for c in cards])
        await update.message.reply_text(
            f"🔮 Я вытянул новый расклад:\n**{card_names}**\n\nСпрашивайте.",
            reply_markup=reply_markup
        )
        return
        
    if text == "🃏 Достать 1 карту":
        cards = draw_cards(1)
        new_card = cards[0]
        session["drawn_cards"].append(new_card)
        
        system_update = f"Вы вытянули дополнительную уточняющую карту: {new_card['name']} ({new_card['orientation']}). Значение: {', '.join(new_card.get('meanings', [])[:3])}"
        history.append({"role": "user", "parts": [f"[SYSTEM: {system_update}]. Прокомментируй её появление и свяжи с предыдущими."]})
        set_tarot_session(user_id, session)
    else:
        history.append({"role": "user", "parts": [text]})
        set_tarot_session(user_id, session)

    # Status message
    status_msg = await update.message.reply_text("🔮 Таролог вглядывается в карты...")

    try:
        tarot_ctx = ""
        for idx, card in enumerate(session["drawn_cards"]):
            tarot_ctx += f"Карта {idx + 1}: {card['name']} ({card['orientation']}) - {', '.join(card.get('meanings', [])[:3])}\n"
            
        sys_prompt = _build_tarot_system_prompt(SpreadType.CLASSIC, tarot_ctx, "")
        sys_prompt += "\nВедите диалог как таролог, отвечая на вопросы пользователя по этому раскладу."
        
        router = get_provider_router()
        response_text, _ = await router.get_response(
            preferred_model="gemini-3.1-flash-lite",
            history=history,
            system_instruction=sys_prompt,
            user_id=user_id,
            chat_id=chat_id,
        )
        
        history.append({"role": "model", "parts": [response_text]})
        set_tarot_session(user_id, session)
        
        formatted_text, parse_mode = TelegramFormatter.format_text(response_text)
        await status_msg.edit_text(formatted_text, parse_mode=parse_mode)
    except Exception as e:
        logging.error("Tarot processing error: %s", e)
        await status_msg.edit_text("Извините, карты затуманены. Попробуйте снова.")

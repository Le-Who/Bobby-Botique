"""
Tarot interactive mode chat handler.
Routes messages to Gemini using Tarot session context.
"""

import logging
import re
import time
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes
from telegram.ext.filters import MessageFilter

from app.handlers.inline import _build_tarot_system_prompt
from app.providers.router import get_provider_router
from app.state import clear_tarot_session, get_tarot_session, is_in_tarot_mode, set_tarot_session
from app.tarot import SpreadType, draw_cards
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter

TAROT_END_SESSION_TEXT = "🛑 Завершить сеанс Таро"
DEFAULT_TAROT_IDLE_CONFIRM_AFTER_SECONDS = 24 * 60 * 60
_TAROT_END_SESSION_RE = re.compile(
    r"^\s*(?:🛑\s*)?завершить\s+сеанс\s+таро\s*$",
    re.IGNORECASE,
)


def is_tarot_end_session_text(text: str | None) -> bool:
    return bool(text and _TAROT_END_SESSION_RE.match(text))


def get_tarot_idle_confirm_after_seconds() -> int:
    try:
        from app.config import settings

        value = getattr(settings, "TAROT_IDLE_CONFIRM_AFTER_SECONDS", DEFAULT_TAROT_IDLE_CONFIRM_AFTER_SECONDS)
    except Exception:
        value = DEFAULT_TAROT_IDLE_CONFIRM_AFTER_SECONDS
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_TAROT_IDLE_CONFIRM_AFTER_SECONDS


def _is_tarot_session_idle(session: dict) -> bool:
    last_activity_at = session.get("last_activity_at")
    if not last_activity_at:
        return True
    return (time.time() - float(last_activity_at)) >= get_tarot_idle_confirm_after_seconds()


class IsTarotMode(MessageFilter):
    def filter(self, message) -> bool:
        if not message or not message.from_user:
            return False
        return is_in_tarot_mode(message.from_user.id)


class IsTarotEndSession(MessageFilter):
    def filter(self, message) -> bool:
        return bool(message and is_tarot_end_session_text(getattr(message, "text", None)))


is_tarot_mode_filter = IsTarotMode()
is_tarot_end_session_filter = IsTarotEndSession()


async def _finish_tarot_session(update: Update, user_id: int) -> None:
    clear_tarot_session(user_id)
    await update.message.reply_text(
        "Сеанс Таро завершен. Возвращаюсь в обычный режим.",
        reply_markup=ReplyKeyboardRemove()
    )


async def _send_idle_route_choice(update: Update, user_id: int, session: dict, text: str) -> None:
    session["pending_idle_text"] = text
    set_tarot_session(user_id, session)

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Продолжить сеанс Таро", callback_data="tarot_idle:continue")],
            [InlineKeyboardButton("Отправить как обычный запрос", callback_data="tarot_idle:llm")],
        ]
    )
    prompt = (
        "Сеанс Таро всё ещё активен, но прошло много времени с последнего сообщения.\n\n"
        "Как обработать ваш новый текст?"
    )
    cleanup_msg = await update.message.reply_text("⏳", reply_markup=ReplyKeyboardRemove())
    await cleanup_msg.edit_text(prompt, reply_markup=keyboard)


@authorized_only
@safe_handler("Произошла ошибка при завершении таро-сеанса")
async def handle_tarot_end_session_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await _finish_tarot_session(update, user_id)


@authorized_only
@safe_handler("Произошла ошибка при обработке таро-запроса")
async def handle_tarot_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text
    chat_id = update.effective_chat.id
    
    if is_tarot_end_session_text(text):
        await _finish_tarot_session(update, user_id)
        return
        
    session = get_tarot_session(user_id)
    if session is None:
        clear_tarot_session(user_id)
        await update.message.reply_text("Сессия не найдена. Начните заново с /tarot", reply_markup=ReplyKeyboardRemove())
        return

    if _is_tarot_session_idle(session):
        await _send_idle_route_choice(update, user_id, session, text)
        return

    session["last_activity_at"] = time.time()
    session.pop("pending_idle_text", None)

    history = session.get("history", [])

    keyboard_full = [
        ["🃏 Достать 1 карту", "🔄 Новый расклад"],
        [TAROT_END_SESSION_TEXT]
    ]
    keyboard_wait = [
        [TAROT_END_SESSION_TEXT]
    ]

    if text == "🔄 Новый расклад":
        session["spread_type"] = SpreadType.CLASSIC.value
        session["drawn_cards"] = []
        session["history"] = []
        session["waiting_for_question"] = True
        set_tarot_session(user_id, session)
        
        reply_markup = ReplyKeyboardMarkup(keyboard_wait, resize_keyboard=True)
        await update.message.reply_text(
            "🔄 Расклад сброшен. Пожалуйста, задайте ваш новый вопрос, и я вытяну карты.",
            reply_markup=reply_markup
        )
        return
        
    if text == "🃏 Достать 1 карту" and not session.get("waiting_for_question"):
        cards = draw_cards(1)
        new_card = cards[0]
        session["drawn_cards"].append(new_card)
        
        system_update = f"Вы вытянули дополнительную уточняющую карту: {new_card['name']} ({new_card['orientation']}). Значение: {', '.join(new_card.get('meanings', [])[:3])}"
        history.append({"role": "user", "parts": [f"[SYSTEM: {system_update}]. Прокомментируй её появление и свяжи с предыдущими."]})
        set_tarot_session(user_id, session)
        
        status_text = "⏳ **Таролог вглядывается в новую карту... Пожалуйста, подождите.**"
        f_text, p_mode = TelegramFormatter.format_text(status_text)
        status_msg = await update.message.reply_text(f_text, parse_mode=p_mode)
    else:
        is_first_question = session.get("waiting_for_question", False)
        history.append({"role": "user", "parts": [text]})
        
        if is_first_question:
            cards = draw_cards(3)
            session["drawn_cards"] = cards
            session["waiting_for_question"] = False
            
            card_names = ", ".join([c["name"] for c in cards])
            status_text = f"⏳ **Таролог вглядывается в карты... Пожалуйста, подождите.**\n\n🔮 *Вытянутые карты:*\n{card_names}"
            
            formatted_text, parse_mode = TelegramFormatter.format_text(status_text)
            status_msg = await update.message.reply_text(
                formatted_text, 
                parse_mode=parse_mode
            )
        else:
            status_text = "⏳ **Таролог обдумывает ваш вопрос... Пожалуйста, подождите.**"
            f_text, p_mode = TelegramFormatter.format_text(status_text)
            status_msg = await update.message.reply_text(f_text, parse_mode=p_mode)
            
        set_tarot_session(user_id, session)

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
        try:
            await status_msg.delete()
        except Exception:
            pass
        reply_markup = ReplyKeyboardMarkup(keyboard_full, resize_keyboard=True)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.error("Tarot processing error: %s", e)
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.message.reply_text("Извините, карты затуманены. Попробуйте снова.")


@authorized_only
@safe_handler("Произошла ошибка при выборе режима обработки")
async def handle_tarot_idle_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    session = get_tarot_session(user_id)
    pending_text = (session or {}).get("pending_idle_text")

    await query.answer()

    if not session or not pending_text:
        await query.edit_message_text("Запрос уже устарел. Напишите его заново.")
        return

    if query.data == "tarot_idle:continue":
        session.pop("pending_idle_text", None)
        session["last_activity_at"] = time.time()
        set_tarot_session(user_id, session)
        await query.edit_message_text("🔮 Продолжаю сеанс Таро...")
        proxy_message = SimpleNamespace(
            text=pending_text,
            from_user=update.effective_user,
            chat=update.effective_chat,
            reply_text=query.message.reply_text,
            photo=(),
            document=None,
            voice=None,
            caption=None,
        )
        proxy_update = SimpleNamespace(
            update_id=getattr(update, "update_id", None),
            message=proxy_message,
            effective_message=proxy_message,
            effective_user=update.effective_user,
            effective_chat=update.effective_chat,
            callback_query=None,
        )
        await handle_tarot_message(proxy_update, context)
        return

    if query.data == "tarot_idle:llm":
        clear_tarot_session(user_id)
        await query.edit_message_text("🤔 Думаю...")
        from app.handlers.agent import process_long_request

        await process_long_request(query.message, update, context, text_override=pending_text)
        return

    await query.edit_message_text("Неизвестный выбор. Напишите запрос заново.")

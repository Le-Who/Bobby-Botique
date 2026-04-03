# /app/handlers/cb_smart_actions.py
"""Callback handlers for Smart Suggestions, Proactive Intent Routing, and Edit Query.

Handles:
    suggest:*       — user tapped a smart suggestion button → inject as draft
    intent_route:*  — user tapped an intent button → route to the correct pipeline
    edit_query      — user tapped "Edit query" on error → pre-fill last message as draft
"""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.ext import ContextTypes


async def suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle smart suggestion button press.

    Uses sendMessageDraft (Bot API 9.5+) to place the suggestion text
    into the user's input field.  If the API is unavailable, falls back
    to sending a hint message.
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # Extract suggestion text from callback_data: "suggest:Расскажи подробнее"
    suggestion_text = (query.data or "").removeprefix("suggest:").strip()
    if not suggestion_text:
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        # sendMessageDraft(chat_id, draft_id, text)
        # draft_id = 0 means "new message draft" (not a reply draft).
        await context.bot.send_message_draft(
            chat_id=chat.id,
            draft_id=0,
            text=suggestion_text,
        )
        logging.info(
            "Smart suggestion draft sent: user=%s text=%r",
            user.id,
            suggestion_text[:50],
        )
    except Exception as exc:
        # PTB version doesn't support send_message_draft or API failed —
        # fall back to posting a hint message that the user can copy.
        logging.debug("send_message_draft failed: %s, falling back to hint", exc)
        try:
            msg = query.message
            if isinstance(msg, Message):
                await msg.reply_text(
                    f"💡 _{suggestion_text}_",
                    parse_mode="Markdown",
                )
        except Exception:
            pass


async def intent_route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle proactive intent routing button press.

    Routes:
        intent_route:draw     → trigger image generation with last user message
        intent_route:research → trigger agentic research
        intent_route:tts      → trigger TTS on the bot's response
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    intent = (query.data or "").removeprefix("intent_route:").strip()
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    user_id = user.id
    msg = query.message
    # Ensure this is a real Message, not InaccessibleMessage
    if not isinstance(msg, Message):
        return

    if intent == "draw":
        await _route_draw(msg, user_id)
    elif intent == "research":
        await _route_research(msg, user_id)
    elif intent == "tts":
        await _route_tts(msg, context)
    else:
        logging.warning("Unknown intent route: %s", intent)


async def _route_draw(msg: Message, user_id: int) -> None:
    """Route to image generation using the last user message as prompt."""
    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state or not chat_state.history:
            await msg.reply_text("❌ Не удалось определить запрос.")
            return

        # Find the last user message
        last_user_msg = ""
        for entry in reversed(chat_state.history):
            if entry.get("role") == "user":
                parts = entry.get("parts", [])
                last_user_msg = parts[0] if parts and isinstance(parts[0], str) else ""
                break

        if not last_user_msg:
            await msg.reply_text("❌ Не найден текст для генерации.")
            return

        # Use sendMessageDraft to pre-fill /draw command
        try:
            await msg.get_bot().send_message_draft(
                chat_id=msg.chat_id,
                draft_id=0,
                text=f"/draw {last_user_msg[:200]}",
            )
        except Exception:
            # Fallback: just tell the user what to type
            await msg.reply_text(
                f"🎨 Скопируйте и отправьте:\n`/draw {last_user_msg[:200]}`",
                parse_mode="Markdown",
            )
    except Exception as e:
        logging.warning("Intent route:draw failed: %s", e)
        await msg.reply_text("❌ Ошибка при подготовке генерации.")


async def _route_research(msg: Message, user_id: int) -> None:
    """Route to agentic research using the last user message."""
    try:
        from app.handlers.ai_search import _handle_research_agent
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state or not chat_state.history:
            await msg.reply_text("❌ Не удалось определить запрос.")
            return

        last_user_msg = ""
        for entry in reversed(chat_state.history):
            if entry.get("role") == "user":
                parts = entry.get("parts", [])
                last_user_msg = parts[0] if parts and isinstance(parts[0], str) else ""
                break

        if not last_user_msg:
            await msg.reply_text("❌ Не найден текст для анализа.")
            return

        status_msg = await msg.reply_text("🔬 Запускаю глубокий анализ...")
        await _handle_research_agent(status_msg, user_id, last_user_msg, chat_state)
    except Exception as e:
        logging.warning("Intent route:research failed: %s", e)
        await msg.reply_text("❌ Ошибка при запуске анализа.")


async def _route_tts(msg: Message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route to TTS — read the bot's response aloud."""
    try:
        response_text = msg.text
        if not response_text:
            await msg.reply_text("❌ Нет текста для озвучивания.")
            return

        from app.voice_engine import fire_voice_reply

        fire_voice_reply(
            bot=context.bot,
            chat_id=msg.chat_id,
            reply_to_message_id=msg.message_id,
            response_text=response_text,
        )
        await msg.reply_text("🎧 Генерирую аудио...")
    except Exception as e:
        logging.warning("Intent route:tts failed: %s", e)
        await msg.reply_text("❌ Ошибка при генерации аудио.")


async def edit_query_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pre-fill the user's last message into their input field for editing.

    Used on error recovery keyboards so the user can quickly fix a typo
    or rephrase their query without retyping from scratch.
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # Retrieve the last sent message from persisted user state
    try:
        from app.state import ensure_state_loaded, get_last_sent_message

        await ensure_state_loaded(user.id)
        last_text = get_last_sent_message(user.id)
    except Exception:
        last_text = None

    if not last_text:
        await query.answer("❌ Нет сохранённого запроса для редактирования", show_alert=True)
        return

    try:
        await context.bot.send_message_draft(
            chat_id=chat.id,
            draft_id=0,
            text=last_text,
        )
    except Exception as exc:
        logging.debug("send_message_draft failed for edit_query: %s", exc)
        # Fallback: show the text so user can copy it
        msg = query.message
        if isinstance(msg, Message):
            # Truncate for display
            display = last_text[:500]
            await msg.reply_text(
                f"✏️ Ваш последний запрос:\n\n`{display}`",
                parse_mode="Markdown",
            )

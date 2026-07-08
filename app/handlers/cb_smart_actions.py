# /app/handlers/cb_smart_actions.py
"""Callback handlers for Smart Suggestions, Proactive Intent Routing, and Edit Query.

Handles:
    suggest:*       — user tapped a smart suggestion button → route to AI agent
    intent_route:*  — user tapped an intent button → route to the correct pipeline
    edit_query      — user tapped "Edit query" on error → pre-fill last message as draft
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Message, Update
from telegram.ext import ContextTypes


async def suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle smart suggestion button press.

    Sends the suggestion text as a visible echo message, then routes it
    through the full AI pipeline (identical to a regular user message).
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # Extract suggestion ID or raw text from callback_data: "suggest:..."
    suggestion_id_or_text = (query.data or "").removeprefix("suggest:").strip()
    if not suggestion_id_or_text:
        return

    from app.utils.response_tags import SUGGESTION_CACHE

    # Try to look up the full text from the cache using the hash ID
    suggestion_text = SUGGESTION_CACHE.get(suggestion_id_or_text)
    if not suggestion_text:
        # If it's a cache miss and looks like an MD5 hash fragment, it's expired
        if len(suggestion_id_or_text) <= 10 and all(c in "0123456789abcdefABCDEF" for c in suggestion_id_or_text):
            try:
                await query.answer("❌ Подсказка устарела. Пожалуйста, напишите запрос вручную.", show_alert=True)
            except Exception:
                pass
            return
        # Fallback for legacy buttons that might have full text in callback_data
        suggestion_text = suggestion_id_or_text

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🗣️ **Вы:** {suggestion_text}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning("Failed to send fake user message: %s", e)

    from app.state import get_user_lock, get_user_state

    user_state = get_user_state(user.id)
    if user_state.is_processing or get_user_lock(user.id).locked():
        # Inform user but don't crash
        try:
            from app.handlers.messages import _send_busy_ephemeral

            await _send_busy_ephemeral(update)
        except Exception:
            pass
        return

    user_state.is_processing = True

    from app.i18n import t as _t
    from app.state import set_last_bot_message, set_last_sent_message

    set_last_sent_message(user.id, suggestion_text)

    try:
        placeholder_message = await context.bot.send_message(chat_id=chat.id, text=_t("msg.thinking", "ru"))
        set_last_bot_message(user.id, placeholder_message.message_id, chat.id)
    except Exception as e:
        logging.error("Failed to send placeholder: %s", e)
        user_state.is_processing = False
        return

    from app.utils.heartbeat import register_heartbeat, stop_heartbeat, unregister_heartbeat

    done_event = asyncio.Event()
    register_heartbeat(placeholder_message.message_id, done_event, update.effective_chat)

    async def _run_suggestion() -> None:
        try:
            async with get_user_lock(user.id):
                from app.handlers.agent import process_long_request

                await process_long_request(placeholder_message, update, context, text_override=suggestion_text)
        except Exception as _e:
            logging.error("Smart suggestion error: %s", _e, exc_info=True)
            try:
                stop_heartbeat(placeholder_message.message_id)
                await placeholder_message.edit_text("❌ При обработке произошла ошибка.")
            except Exception:
                pass
        finally:
            _us = get_user_state(user.id)
            _us.is_processing = False
            unregister_heartbeat(placeholder_message.message_id)
            if not done_event.is_set():
                stop_heartbeat(placeholder_message.message_id)

    from app.utils.background_tasks import submit_task

    submit_task(_run_suggestion())


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

        # Resolve the user's configured voice preference
        _user_id = msg.from_user.id if msg.from_user else getattr(context, "_user_id", 0)
        tts_voice = "Aoede"
        tts_temperature_val: float | None = None
        if _user_id:
            from app.repos.chats import get_user_chat

            chat_state = await get_user_chat(_user_id)
            tts_voice = chat_state.voice_id or "Aoede"
            tts_temperature_val = chat_state.tts_temperature

        from app.voice_engine import fire_voice_reply
        from app.voice_intent import build_voice_source_key

        await fire_voice_reply(
            bot=context.bot,
            user_id=int(_user_id),
            chat_id=msg.chat_id,
            reply_to_message_id=msg.message_id,
            response_text=response_text,
            voice=tts_voice,
            tts_temperature=tts_temperature_val,
            source_key=build_voice_source_key("smart_tts", msg.chat_id, msg.message_id),
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

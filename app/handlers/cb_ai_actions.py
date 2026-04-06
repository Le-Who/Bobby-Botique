"""
Callback handlers — heavy AI actions.

Handles complex_search, fallback, and retry_last callbacks.
These are semaphore-guarded and run in background tasks.
"""

__all__ = [
    "complex_search_callback",
    "continue_stream_callback",
    "fallback_callback",
    "retry_last_callback",
    "tts_reply_callback",
]

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app import state
from app.handlers import agent
from app.handlers.callbacks import (
    _BUSY_TOAST,
    _HEAVY_CALLBACK_SEMAPHORE,
    _background_tasks,
)
from app.i18n import t
from app.repos.chats import get_user_chat
from app.request_context import set_request_id, set_user_context


async def complex_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    action = query.data.split(":")[1]
    placeholder_message = query.message

    if action == "cancel":
        await query.answer()
        await placeholder_message.delete()
        return

    # Get оригинальное message from contextа or from reply_to_message
    original_message = None
    if hasattr(context, "user_data") and context.user_data is not None and "original_message" in context.user_data:
        original_message = context.user_data["original_message"]
    else:
        original_message = query.message.reply_to_message

    if not original_message:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await placeholder_message.edit_text(
            t("error.original_not_found"),
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    user_id = original_message.from_user.id
    user_lock = state.get_user_lock(user_id)

    # P4: single query.answer() — Telegram ignores subsequent calls per callback_query_id
    await query.answer(_BUSY_TOAST if user_lock.locked() else "")
    if user_lock.locked():
        return

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    # 1. Определяем, какую задачу будем запускать.
    task_to_run = None
    if action == "vision_only":
        # 2. СРАЗУ даем обратную связь пользователю.
        await placeholder_message.edit_text(t("processing.describing_image"))
        chat_state = await get_user_chat(user_id)
        task_to_run = agent._handle_photo(placeholder_message, original_message, chat_state)  # type: ignore[arg-type]  # message comes from original update
    elif action == "confirm":
        # У этой функции своя обратная связь ("Аналfromирую..."), поэтому здесь ничего не меняем.
        search_prefix = "??" if (original_message.caption and original_message.caption.startswith("??")) else "?"
        task_to_run = agent._handle_complex_agent_search(placeholder_message, original_message, search_prefix)

    # 3. If задача определена, запускаем ее в фоне под блокировкой.
    if task_to_run:

        async def task_wrapper() -> None:
            try:
                from app.adapters.concurrency import ultra_heavy_semaphore

                async with ultra_heavy_semaphore, user_lock:
                    await task_to_run
            except Exception as e:
                logging.error("complex_search task failed: %s", e, exc_info=True)
                with contextlib.suppress(Exception):
                    await placeholder_message.edit_text(t("error.generic"))

        _task = asyncio.create_task(task_wrapper())
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)


async def fallback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    model_override = parts[2] if len(parts) > 2 else ""
    placeholder_message = query.message

    if action == "cancel":
        await query.answer()
        await placeholder_message.edit_text(t("btn.operation_cancelled"))
        return

    # Get оригинальное message from contextа or from reply_to_message
    original_message = None
    if hasattr(context, "user_data") and context.user_data is not None and "original_message" in context.user_data:
        original_message = context.user_data["original_message"]
    else:
        original_message = query.message.reply_to_message

    if not original_message:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await placeholder_message.edit_text(
            t("error.original_not_found"),
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    user_id = original_message.from_user.id
    user_lock = state.get_user_lock(user_id)

    # P4: single query.answer() — Telegram ignores subsequent calls per callback_query_id
    await query.answer(_BUSY_TOAST if user_lock.locked() else "")
    if user_lock.locked():
        return

    async def task_wrapper() -> None:
        try:
            async with _HEAVY_CALLBACK_SEMAPHORE, user_lock:
                if action == "confirm":
                    chat_state = await get_user_chat(user_id)
                    user_message = original_message.text

                    # Dynamically check for explicit voice requests
                    _lower = user_message.lower() if user_message else ""
                    reply_with_voice = (
                        "озвучь ответ" in _lower or "ответь голосом" in _lower or "прочитай вслух" in _lower
                    )

                    await agent._handle_regular_chat(
                        placeholder_message,  # type: ignore[arg-type]  # MaybeInaccessibleMessage
                        user_id,
                        user_message,
                        chat_state,
                        model_override=model_override,
                        reply_with_voice=reply_with_voice,
                    )
        except Exception as e:
            logging.error("fallback task failed: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await placeholder_message.edit_text(t("error.generic"))

    _task = asyncio.create_task(task_wrapper())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)


async def retry_last_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Повтор последнего пользовательского запроса по кнопке."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )
    user_id = query.from_user.id

    # Hydrate persisted state from DB
    from app.state import ensure_state_loaded, get_last_sent_message

    await ensure_state_loaded(user_id)

    chat_state = await get_user_chat(user_id)
    last_text = None
    try:
        last_text = get_last_sent_message(user_id)
    except Exception:
        last_text = None
    if not last_text:
        from app.utils.keyboards import error_with_back_keyboard

        await query.answer()
        await query.edit_message_text(
            t("error.no_retry_data"),
            reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
        )
        return

    # P4+P5: single query.answer() with toast if busy; check BEFORE creating placeholder
    user_lock = state.get_user_lock(user_id)
    await query.answer(_BUSY_TOAST if user_lock.locked() else "")
    if user_lock.locked():
        return

    # Create плейсхолдер и запускаем обычную обработку как on новом сообщении
    placeholder_message = await query.message.reply_text(t("processing.retry"))
    from app.handlers.agent import _handle_regular_chat

    async def _retry_wrapper() -> None:
        try:
            async with _HEAVY_CALLBACK_SEMAPHORE, user_lock:
                await _handle_regular_chat(placeholder_message, user_id, last_text, chat_state)
        except Exception as e:
            logging.error("retry_last_callback failed: %s", e, exc_info=True)
            try:
                from app.utils.keyboards import error_with_back_keyboard

                await placeholder_message.edit_text(
                    t("error.retry_failed"),
                    reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
                )
            except Exception:
                pass

    _task = asyncio.create_task(_retry_wrapper())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)


async def tts_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate voice reply for the current AI response (🔊 Озвучить button)."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )
    await query.answer("🔊 Генерирую голос...")

    # Extract text from the message that has the button
    response_text = query.message.text if query.message else None

    # Check if there's a WebApp url with a UID (Long-read)
    if query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.web_app and "/reader?id=" in btn.web_app.url:
                    uid = btn.web_app.url.split("/reader?id=")[1].split("&")[0]
                    try:
                        from app.cache import get_long_message

                        full_text = await get_long_message(uid)
                        if full_text:
                            response_text = full_text
                            logging.info(
                                "tts_reply_callback: Fetched full text (%d chars) for uid=%s", len(full_text), uid
                            )
                    except Exception as e:
                        logging.warning("tts_reply_callback: Failed to load long message from cache: %s", e)
                    break

    if not response_text or len(response_text.strip()) < 5:
        return

    # Strip code blocks (triple backticks) for TTS so we don't synthesize raw code visually represented by the code block
    import re

    response_text = re.sub(r"```.*?```", "", response_text, flags=re.DOTALL)

    chat_id = query.message.chat_id
    message_id = query.message.message_id

    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(query.from_user.id)
        from app.voice_engine import fire_voice_reply

        fire_voice_reply(
            bot=query.get_bot(),
            chat_id=chat_id,
            reply_to_message_id=message_id,
            response_text=response_text,
            voice=chat_state.voice_id or "Aoede",
            tts_temperature=chat_state.tts_temperature,
        )
    except Exception as e:
        logging.error("TTS reply callback failed: %s", e)


# ── Interruption error footers to strip from partial text ──────────────────
_INTERRUPTION_FOOTERS = (
    "\n\n⏰ _(ответ был прерван по таймауту)_",
    "\n\n⚠️ _(ответ был прерван из-за ошибки сервера)_",
    "\n\n⚠️ _(ответ был прерван из-за непредвиденной ошибки)_",
    # Legacy footers (backward compat)
    "\n\n⚠️ _(ответ был прерван из-за ошибки API)_",
)


def _strip_interruption_footer(text: str) -> str:
    """Remove the machine-appended interruption footer from partial text."""
    for footer in _INTERRUPTION_FOOTERS:
        if text.endswith(footer):
            return text[: -len(footer)]
    return text


async def continue_stream_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Continue generating a response from where it was interrupted (▶️ Продолжить button)."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )
    user_id = query.from_user.id

    # Extract partial text from the interrupted message
    partial_text = query.message.text if query.message else None
    if not partial_text or len(partial_text.strip()) < 10:
        await query.answer("❌")
        return

    # Strip the interruption footer to get clean partial text
    clean_partial = _strip_interruption_footer(partial_text)

    # P4+P5: single query.answer() with toast if busy
    user_lock = state.get_user_lock(user_id)
    await query.answer(_BUSY_TOAST if user_lock.locked() else "")
    if user_lock.locked():
        return

    chat_state = await get_user_chat(user_id)

    # Inject the partial text as model output so the LLM has context
    chat_state.history.append({"role": "model", "parts": [clean_partial]})

    from app.metrics import role_conv_metrics
    from app.utils.background_tasks import submit_task

    submit_task(role_conv_metrics.record_stream_recovery())

    # The continuation prompt — instructs the model to seamlessly pick up
    continuation_prompt = (
        "Пожалуйста, продолжи прерванную мысль с того места, где ты остановился, с учётом контекста уже написанного."
    )
    chat_state.history.append({"role": "user", "parts": [continuation_prompt]})

    # Create placeholder for the continuation response
    placeholder_message = await query.message.reply_text(t("processing.continuing"))

    from app.handlers.agent import _handle_regular_chat

    async def _continue_wrapper() -> None:
        try:
            async with _HEAVY_CALLBACK_SEMAPHORE, user_lock:
                await _handle_regular_chat(
                    placeholder_message,
                    user_id,
                    continuation_prompt,
                    chat_state,
                )
        except Exception as e:
            logging.error("continue_stream_callback failed: %s", e, exc_info=True)
            try:
                from app.utils.keyboards import error_with_back_keyboard

                await placeholder_message.edit_text(
                    t("error.retry_failed"),
                    reply_markup=error_with_back_keyboard("start_menu", "⬅️ Меню"),
                )
            except Exception:
                pass

    _task = asyncio.create_task(_continue_wrapper())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)

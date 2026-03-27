"""
Callback handlers — heavy AI actions.

Handles complex_search, fallback, and retry_last callbacks.
These are semaphore-guarded and run in background tasks.
"""

__all__ = ["complex_search_callback", "fallback_callback", "retry_last_callback"]

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
                    await placeholder_message.edit_text(
                        t("error.generic")
                    )

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
                    await agent._handle_regular_chat(
                        placeholder_message,  # type: ignore[arg-type]  # MaybeInaccessibleMessage
                        user_id,
                        user_message,
                        chat_state,
                        model_override=model_override,
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

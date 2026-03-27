# /app/handlers/messages.py
"""Thin message router — dispatches incoming messages to specialized sub-modules.

Sub-modules:
    msg_media    — media group accumulation, deferred processing
    msg_roles    — role creation (AI / manual), role/conversation rename
    msg_document — document upload, document-mode Q&A
"""

import asyncio
import contextlib
import logging

from telegram import Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app import state
from app.config import settings
from app.handlers.msg_document import handle_document, handle_document_mode_interaction
from app.handlers.msg_media import (
    MEDIA_GROUPS,
    MEDIA_GROUPS_MAX_SIZE,
    MEDIA_GROUPS_TTL,
    cleanup_old_media_groups,
    process_media_group_update,
)
from app.handlers.msg_roles import (
    handle_conversation_rename,
    handle_custom_role_generation,
    handle_edit_prompt,
    handle_manual_role_input,
    handle_role_rename,
)
from app.handlers.msg_voice import handle_voice_inline
from app.metrics import metrics_collector
from app.repos.chats import get_user_chat
from app.repos.users import is_authorized
from app.request_context import set_request_id, set_user_context
from app.security import check_user_rate_limit
from app.tracing import bind_request_span
from app.utils.api_logger import api_logger
from app.utils.heartbeat import register_heartbeat, stop_heartbeat, unregister_heartbeat


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main message router — dispatches to specialized sub-modules."""
    if not update or not update.effective_user:
        logging.debug(
            "Skipping update without effective_user (update_id=%s)",
            getattr(update, "update_id", "?"),
        )
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Hydrate persisted user state from DB (lazy, fast no-op if already loaded)
    from app.state import ensure_state_loaded

    await ensure_state_loaded(user_id)

    request_id = set_request_id(f"tgmsg-{chat_id}-{getattr(update, 'update_id', 'na')}")
    set_user_context(user_id, chat_id)

    # Immediate typing indicator — instant feedback before any processing
    try:
        await update.effective_chat.send_action(action="typing")
    except Exception:
        pass  # Non-critical

    # Correlation contract: request_id is propagated as trace_id baseline.
    with bind_request_span(request_id, span_name="telegram-message"):
        if not isinstance(user_id, int) or user_id <= 0:
            logging.error("Invalid user_id: %s", user_id)
            return

        # ── 1. Media groups ──────────────────────────────────────────────────
        if await process_media_group_update(update, context, user_id, chat_id):
            return

        # ── 2. Telegram API logging ──────────────────────────────────────────
        message_type = "photo" if update.message.photo else "text" if update.message.text else "other"
        start_time = api_logger.log_request(
            "telegram",
            method="handle_message",
            message_type=message_type,
        )

        # ── 3. Validation ────────────────────────────────────────────────────
        message_text = update.message.text if update.message and update.message.text else "No text"
        if len(message_text) > settings.TELEGRAM_MESSAGE_LIMIT:
            logging.warning("Message too long from user %s: %d chars", user_id, len(message_text))
            await update.message.reply_text(
                "❌ Сообщение слишком длинное. Максимум 4096 символов.\nСократите текст и отправьте снова."
            )
            return

        logging.info("Received message from user %s: %s", user_id, message_text[:100])

        if not await check_user_rate_limit(user_id):
            logging.warning("Rate limit exceeded for user %s", user_id)
            await update.message.reply_text(
                "⏱️ Превышен лимит запросов. Пожалуйста, подождите немного перед следующим запросом."
            )
            return

        # ── 3b. Request dedup (double-tap prevention) ────────────────────────
        if update.message and update.message.text:
            from app.middleware.dedup import is_duplicate_request

            if await is_duplicate_request(user_id, update.message.text):
                logging.info("Dedup: skipping duplicate from user %s", user_id)
                return

        if not await is_authorized(user_id):
            logging.warning("Unauthorized user %s attempted to use bot", user_id)
            return

        # ── 4. Document uploads ──────────────────────────────────────────────
        if update.message.document:
            logging.info(
                "Processing document from user %s: %s",
                user_id,
                update.message.document.file_name,
            )
            await handle_document(update, context)
            return

        # ── 4b. Voice messages ───────────────────────────────────────────────
        if update.message.voice:
            logging.info("Processing voice message from user %s", user_id)
            await handle_voice_inline(update, context)
            return

        # ── 5. State-machine dispatchers (role/rename flows) ─────────────────
        if await handle_role_rename(update, context, user_id):
            return
        if await handle_edit_prompt(update, context, user_id):
            return
        if await handle_conversation_rename(update, context, user_id):
            return
        if await handle_manual_role_input(update, context, user_id):
            return
        if await handle_custom_role_generation(update, context, user_id, chat_id, message_text):
            return

        # ── 6. Document mode ─────────────────────────────────────────────────
        if await handle_document_mode_interaction(update, context, user_id):
            return

        # ── 7. Save last user input for retry button ─────────────────────────
        try:
            from app.state import set_last_sent_message

            if update.message and update.message.text:
                set_last_sent_message(user_id, update.message.text)
        except Exception:
            logging.exception("Error saving last sent message text")

        # ── 8. Create placeholder & heartbeat, then process AI request ───────
        is_photo = bool(update.message.photo)

        if is_photo:
            logging.info("Processing single photo from user %s", user_id)
            placeholder_message = await update.message.reply_text("🖼️ Обрабатываю изображение...")
        else:
            logging.info("Processing text message from user %s", user_id)
            placeholder_message = await update.message.reply_text("🤔 Думаю...")

        done_event = asyncio.Event()
        register_heartbeat(placeholder_message.message_id, done_event, update.effective_chat)

        async def task_wrapper() -> None:
            try:
                # Lock inversion fix: acquire user lock BEFORE the global semaphore.
                # This ensures a single user doesn't consume all global slots while waiting for their own lock.
                # Semaphore is now acquired inside process_long_request based on request classification.
                async with state.get_user_lock(user_id):
                    logging.info("Starting task processing for user %s", user_id)

                    try:
                        from app.handlers.agent import process_long_request

                        await process_long_request(placeholder_message, update, context)
                    except ImportError:
                        stop_heartbeat(placeholder_message.message_id)
                        await placeholder_message.edit_text("🤔 Обрабатываю ваш запрос... (упрощенный режим)")

                    stop_heartbeat(placeholder_message.message_id)

                    logging.info("Completed task processing for user %s", user_id)

                    import time as _time

                    elapsed = _time.time() - start_time
                    api_logger.log_response(
                        "telegram",
                        start_time,
                        method="handle_message",
                    )
                    await metrics_collector.record_request("handle_message", elapsed, success=True, user_id=user_id)

            except Exception as e:
                logging.error("Error in task wrapper for user %s: %s", user_id, e, exc_info=True)
                try:
                    stop_heartbeat(placeholder_message.message_id)
                    from app.errors import build_retry_and_roles_keyboard

                    await placeholder_message.edit_text(
                        "❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
                        reply_markup=build_retry_and_roles_keyboard(),
                    )
                except (BadRequest, NetworkError) as edit_error:
                    logging.error("Could not edit placeholder message: %s", edit_error)

                import time as _time

                elapsed = _time.time() - start_time
                api_logger.log_response(
                    "telegram",
                    start_time,
                    method="handle_message",
                    success=False,
                    error_message=str(e),
                )
                await metrics_collector.record_request("handle_message", elapsed, success=False, user_id=user_id)
            finally:
                unregister_heartbeat(placeholder_message.message_id)
                if not done_event.is_set():
                    stop_heartbeat(placeholder_message.message_id)

        from app.utils.background_tasks import submit_task

        submit_task(task_wrapper())


def register(application: Application) -> None:
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request))
    application.add_handler(MessageHandler(filters.PHOTO, handle_request))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_request))
    application.add_handler(MessageHandler(filters.VOICE, handle_request))

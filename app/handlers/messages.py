# /app/handlers/messages.py
"""Thin message router — dispatches incoming messages to specialized sub-modules.

Sub-modules:
    msg_media    — media group accumulation, deferred processing
    msg_roles    — role creation (AI / manual), role/conversation rename
    msg_document — document upload, document-mode Q&A

Key architectural decisions:
    - All handlers use ``update.effective_message`` instead of ``update.message``
      so filters work safely across message/edited_message/channel_post update types.
    - ``handle_request``        — new messages only (filters.UpdateType.MESSAGE)
    - ``handle_edited_request`` — edited messages (filters.UpdateType.EDITED_MESSAGE)
      Cancels any in-flight AI task and edits the original bot reply in-place (no new message).
"""

import asyncio
import logging

from telegram import Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app import state
from app.config import settings
from app.handlers.cmd_image import _get_draw_state, _run_generation, check_draw_intent
from app.handlers.msg_document import handle_document, handle_document_mode_interaction
from app.handlers.msg_media import (
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
from app.repos.users import is_authorized
from app.request_context import set_request_id, set_user_context
from app.security import check_user_rate_limit
from app.tracing import bind_request_span
from app.utils.api_logger import api_logger
from app.utils.heartbeat import register_heartbeat, stop_heartbeat, unregister_heartbeat


async def _send_busy_ephemeral(update: Update) -> None:
    """Send localized busy toast that self-destructs after 4s."""
    from app.i18n import detect_language as _dl
    from app.i18n import t as _t

    # Use effective_message — works for both new and edited message contexts
    msg_obj = update.effective_message
    text = msg_obj.text if msg_obj else None
    lang = _dl(text)
    if not msg_obj:
        return
    try:
        busy_msg = await msg_obj.reply_text(_t("busy.toast", lang))

        async def _del() -> None:
            await asyncio.sleep(4)
            try:
                await busy_msg.delete()
            except Exception:
                pass

        from app.utils.background_tasks import submit_task

        submit_task(_del())
    except Exception as e:
        logging.warning("Failed to send busy toast: %s", e)


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main message router — handles NEW messages only.

    Registered with ``filters.UpdateType.MESSAGE`` so ``update.message``
    is guaranteed non-None. Uses ``effective_message`` for forward-compatibility.
    """
    if not update or not update.effective_user:
        logging.debug(
            "Skipping update without effective_user (update_id=%s)",
            getattr(update, "update_id", "?"),
        )
        return

    # Safe: we are registered only for UpdateType.MESSAGE
    effective_msg = update.effective_message
    if not effective_msg:
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
        message_type = (
            "photo"
            if effective_msg.photo
            else "voice"
            if effective_msg.voice
            else "text"
            if effective_msg.text
            else "other"
        )
        start_time = api_logger.log_request(
            "telegram",
            method="handle_message",
            message_type=message_type,
        )

        # ── 3. Validation ────────────────────────────────────────────────────
        message_text = effective_msg.text if effective_msg.text else "No text"
        if len(message_text) > settings.TELEGRAM_MESSAGE_LIMIT:
            logging.warning("Message too long from user %s: %d chars", user_id, len(message_text))
            from app.i18n import t

            await effective_msg.reply_text(t("error.message_too_long"))
            return

        logging.info("Received message from user %s: %s", user_id, message_text[:100])

        if not await check_user_rate_limit(user_id):
            logging.warning("Rate limit exceeded for user %s", user_id)
            from app.i18n import t

            await effective_msg.reply_text(t("error.rate_limit"))
            return

        # ── 3b. Collaborative board reply aggregation ────────────────────────
        # Must be intercepted BEFORE dedup/auth guards because board replies
        # arrive via Telegram's via_bot privacy exception (no admin rights needed)
        # and are not regular user-to-bot messages.
        try:
            from app.handlers.board_handler import try_handle_board_reply

            if await try_handle_board_reply(update, context):
                return
        except Exception as _board_err:
            logging.debug("Board reply check skipped: %s", _board_err)

        # ── 3c. Request dedup (double-tap prevention) ────────────────────────
        if effective_msg.text:
            from app.middleware.dedup import is_duplicate_request

            if await is_duplicate_request(user_id, effective_msg.text):
                logging.info("Dedup: skipping duplicate from user %s", user_id)
                return

        if not await is_authorized(user_id):
            logging.warning("Unauthorized user %s attempted to use bot", user_id)
            return

        # ── 4. Document uploads ──────────────────────────────────────────────
        if effective_msg.document:
            logging.info(
                "Processing document from user %s: %s",
                user_id,
                effective_msg.document.file_name,
            )
            await handle_document(update, context)
            return

        # ── 4b. Voice messages ───────────────────────────────────────────────
        if effective_msg.voice:
            logging.info("Processing voice message from user %s", user_id)

            # Dedup guard (prevents double-processing from Telegram retries)
            from app.middleware.dedup import is_duplicate_voice

            if await is_duplicate_voice(user_id, effective_msg.voice.file_unique_id):
                logging.debug("Voice dedup: skipping duplicate voice for user %s", user_id)
                return

            # Duration guard (skip very short accidental recordings)
            if int(getattr(effective_msg.voice, "duration", 0)) < 1:
                from app.i18n import detect_language as _dl
                from app.i18n import t as _t

                await effective_msg.reply_text(_t("voice.too_short", _dl(None)))
                return

            user_state = state.get_user_state(user_id)
            if user_state.is_processing or state.get_user_lock(user_id).locked():
                await _send_busy_ephemeral(update)
                return
            user_state.is_processing = True

            from app.i18n import t as _t

            placeholder_message = await effective_msg.reply_text(_t("voice.processing", "ru"))

            done_event = asyncio.Event()
            register_heartbeat(placeholder_message.message_id, done_event, update.effective_chat)

            async def voice_task_wrapper() -> None:
                try:
                    async with state.get_user_lock(user_id):
                        logging.info("Starting voice processing for user %s", user_id)

                        await handle_voice_inline(placeholder_message, update, context)

                        stop_heartbeat(placeholder_message.message_id)
                        logging.info("Completed voice processing for user %s", user_id)

                        import time as _time

                        elapsed = _time.time() - start_time
                        api_logger.log_response(
                            "telegram",
                            start_time,
                            method="handle_message",
                        )
                        await metrics_collector.record_request(
                            "handle_message",
                            elapsed,
                            success=True,
                            user_id=user_id,
                        )

                except Exception as e:
                    logging.error("Error in voice task wrapper for user %s: %s", user_id, e, exc_info=True)
                    try:
                        stop_heartbeat(placeholder_message.message_id)
                        from app.errors import build_retry_and_roles_keyboard
                        from app.i18n import t

                        await placeholder_message.edit_text(
                            t("error.generic"),
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
                    await metrics_collector.record_request(
                        "handle_message",
                        elapsed,
                        success=False,
                        user_id=user_id,
                    )
                finally:
                    user_state = state.get_user_state(user_id)
                    user_state.is_processing = False
                    unregister_heartbeat(placeholder_message.message_id)
                    if not done_event.is_set():
                        stop_heartbeat(placeholder_message.message_id)

            from app.utils.background_tasks import submit_task

            submit_task(voice_task_wrapper())
            return

        # ── 5. State-machine dispatchers (role/rename flows) ─────────────────

        # ── 5a. Voice edit interception ──────────────────────────────────────
        # If the user clicked "Edit" on a voice transcript, capture corrected text
        if context.user_data and context.user_data.get("voice_edit_pending"):
            pending = context.user_data.get("voice_pending")
            if pending and message_text:
                context.user_data.pop("voice_edit_pending", None)
                context.user_data.pop("voice_pending", None)
                # Route corrected text through AI chat as a normal message
                corrected_text = message_text
                logging.info("Voice edit: user %s sent corrected text", user_id)
                # Fall through to normal text processing with the corrected text
                message_text = corrected_text
                # Continue to normal text handling below
        # ── 5b. Draw prompt input interception ──────────────────────────────
        # If the user previously pressed "✏️ Изменить промпт", capture text here.
        from app.handlers.cmd_image import handle_draw_prompt_input

        if await handle_draw_prompt_input(update, context):
            return

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

            if effective_msg.text:
                set_last_sent_message(user_id, effective_msg.text)
        except Exception:
            logging.exception("Error saving last sent message text")

        # ── 7b. Debounce rapid-fire text/forward messages ────────────────────
        # Aggregation window: merges multi-message "split taps" and forwarded
        # bursts into a single AI request, preserving author attribution,
        # original timestamps, and a clean user-instruction / forwarded-content
        # split.  Applies to ALL non-photo, non-voice, non-document text.
        is_photo = bool(effective_msg.photo)
        if not is_photo and not effective_msg.voice and not effective_msg.document:
            from app.middleware.debounce import DebounceResult, debounce_message

            debounce_result = await debounce_message(
                user_id,
                effective_msg,
                bot=context.bot,
            )
            if debounce_result is None:
                # Absorbed — the first caller in this window will process the
                # merged result when the window fires.
                return
            # Build a structured, author-attributed context block for the LLM.
            message_text = debounce_result.build_llm_context()

            # ── Persist forwarded-batch metadata for downstream handlers ──────
            # ai_chat uses this to show the "💾 Сохранить тезисы" memory button.
            # agent.py uses photo_messages to route to multimodal handler.
            if context.user_data is not None:
                context.user_data["_fwd_batch"] = debounce_result.has_forwarded_content
                fwd_photos = debounce_result.forwarded_photo_messages
                if fwd_photos:
                    context.user_data["_fwd_photos"] = fwd_photos
                else:
                    context.user_data.pop("_fwd_photos", None)
            else:
                fwd_photos = []

        # ── 7c. Implicit image generation intent ─────────────────────────────
        # Matches: "Бот, нарисуй..." / "изобрази..." / "сгенерируй картинку..."
        # Bypasses conversational AI; routes straight to Canvas 2.0 pipeline.
        from app.handlers.cmd_image import check_draw_intent_async

        _draw_prompt = await check_draw_intent_async(message_text)
        if _draw_prompt:
            logging.info("Draw intent detected for user %s: %r", user_id, _draw_prompt[:60])
            user_state = state.get_user_state(user_id)
            if user_state.is_processing or state.get_user_lock(user_id).locked():
                await _send_busy_ephemeral(update)
                return
            user_state.is_processing = True

            draw_placeholder = await effective_msg.reply_text("🎨 Рисую... это займёт несколько секунд.")

            async def _draw_task_wrapper(
                _prompt=_draw_prompt,
                _ph=draw_placeholder,
                _uid=user_id,
                _st=start_time,
            ) -> None:
                try:
                    async with state.get_user_lock(_uid):
                        _ds = _get_draw_state(context)
                        # Temporarily suppress heartbeat — image pipeline sends
                        # its own typing heartbeat via ChatAction.UPLOAD_PHOTO.
                        try:
                            await _ph.delete()
                        except Exception:
                            pass
                        await _run_generation(
                            update,
                            context,
                            prompt=_prompt,
                            model=_ds["model"],
                            aspect_ratio=_ds["aspect_ratio"],
                            enhance=_ds.get("enhance_prompt", False),
                        )
                        import time as _time

                        elapsed = _time.time() - _st
                        api_logger.log_response("telegram", _st, method="handle_message")
                        await metrics_collector.record_request("handle_message", elapsed, success=True, user_id=_uid)
                except Exception as _e:
                    logging.error("Error in draw task wrapper for user %s: %s", _uid, _e, exc_info=True)
                    import time as _time

                    elapsed = _time.time() - _st
                    api_logger.log_response(
                        "telegram", _st, method="handle_message", success=False, error_message=str(_e)
                    )
                    await metrics_collector.record_request("handle_message", elapsed, success=False, user_id=_uid)
                finally:
                    _us = state.get_user_state(_uid)
                    _us.is_processing = False

            from app.utils.background_tasks import submit_task

            submit_task(_draw_task_wrapper())
            return

        # ── 8. Create placeholder & heartbeat, then process AI request ───────
        user_state = state.get_user_state(user_id)
        if user_state.is_processing or state.get_user_lock(user_id).locked():
            # ── Network-Stall Cancellation (Plan §7) ─────────────────────
            # If the previous task is stuck waiting for HTTP headers (TTFB >15s),
            # cancel it and process the new message. If the task is healthy
            # (actively streaming/searching), show the usual busy toast.
            if state.is_task_stalled(user_id):
                was_cancelled = state.cancel_active_task(user_id)
                if was_cancelled:
                    logging.info(
                        "Network-stall cancellation: cancelled stalled task for user %s (new message arrived)",
                        user_id,
                    )
                    # Brief yield for the cancelled task's finally-block to release user_lock
                    await asyncio.sleep(0.15)
                    # Re-check: lock should be released now
                    if state.get_user_lock(user_id).locked():
                        await _send_busy_ephemeral(update)
                        return
                else:
                    await _send_busy_ephemeral(update)
                    return
            else:
                await _send_busy_ephemeral(update)
                return
        user_state.is_processing = True

        # ── Intent Direct Routing (Plan §4) ──────────────────────────────
        # For text-only messages, try resolving via lightweight APIs
        # (weather, currency) before consuming an LLM call.
        if not is_photo and message_text:
            try:
                from app.intent_router import try_direct_intent

                intent_result = await try_direct_intent(message_text)
                if intent_result and intent_result.handled:
                    user_state.is_processing = False
                    await effective_msg.reply_text(
                        intent_result.text,
                        parse_mode="Markdown",
                    )
                    state.set_last_sent_message(user_id, message_text)
                    logging.info("Intent direct routing handled for user %s", user_id)
                    return
            except Exception as e:
                logging.debug("Intent routing failed (falling back to LLM): %s", e)

        if is_photo:
            logging.info("Processing single photo from user %s", user_id)
            from app.i18n import t as _t

            placeholder_message = await effective_msg.reply_text(_t("msg.processing_image"))
        else:
            logging.info("Processing text message from user %s", user_id)
            from app.i18n import t as _t

            placeholder_message = await effective_msg.reply_text(_t("msg.thinking"))

        # Track this placeholder so edited_message handler can reuse it
        state.set_last_bot_message(user_id, placeholder_message.message_id, chat_id)

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

                        await process_long_request(
                            placeholder_message,
                            update,
                            context,
                            text_override=message_text,
                        )
                    except ImportError:
                        stop_heartbeat(placeholder_message.message_id)
                        from app.i18n import t

                        await placeholder_message.edit_text(t("processing.simplified"))

                    stop_heartbeat(placeholder_message.message_id)

                    # Keep last-bot-message current after agent finishes editing in-place
                    state.set_last_bot_message(user_id, placeholder_message.message_id, chat_id)

                    logging.info("Completed task processing for user %s", user_id)

                    import time as _time

                    elapsed = _time.time() - start_time
                    api_logger.log_response(
                        "telegram",
                        start_time,
                        method="handle_message",
                    )
                    await metrics_collector.record_request("handle_message", elapsed, success=True, user_id=user_id)

            except asyncio.CancelledError:
                # Task was cancelled because user edited the message — clean up silently
                logging.info("task_wrapper: cancelled for user %s (edit supersede)", user_id)
                stop_heartbeat(placeholder_message.message_id)
            except Exception as e:
                logging.error("Error in task wrapper for user %s: %s", user_id, e, exc_info=True)
                try:
                    stop_heartbeat(placeholder_message.message_id)
                    from app.errors import build_retry_and_roles_keyboard
                    from app.i18n import t

                    await placeholder_message.edit_text(
                        t("error.generic"),
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
                user_state = state.get_user_state(user_id)
                user_state.is_processing = False
                state.clear_active_task(user_id)
                unregister_heartbeat(placeholder_message.message_id)
                if not done_event.is_set():
                    stop_heartbeat(placeholder_message.message_id)

        from app.utils.background_tasks import submit_task

        ai_task = submit_task(task_wrapper())
        state.register_active_task(user_id, ai_task)


async def handle_edited_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle edited messages (UpdateType.EDITED_MESSAGE).

    UX-first in-place edit flow:
    1. Cancel the inflight AI task if one is running (user corrected before response arrived).
    2. Find the bot's previous placeholder / response message.
    3. Edit that message in-place to "думаю заново..." — no new message, chat stays clean.
    4. Kick off a fresh AI request with the corrected text.

    Falls back to a fresh reply if the previous bot message is unavailable.
    """
    if not update.effective_user or not update.edited_message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    edited_msg = update.edited_message

    # Only handle plain text edits (photo/voice edits are uncommon and use new messages)
    if not edited_msg.text:
        return

    # Re-apply rate-limit & auth guards
    if not await check_user_rate_limit(user_id):
        return
    if not await is_authorized(user_id):
        return

    from app.state import ensure_state_loaded

    await ensure_state_loaded(user_id)

    new_text = edited_msg.text.strip()
    logging.info("edited_message from user %s: %r", user_id, new_text[:80])

    # ── Cancel any inflight task ──────────────────────────────────────────────
    was_cancelled = state.cancel_active_task(user_id)
    if was_cancelled:
        logging.info("edit: cancelled inflight task for user %s", user_id)
        # Brief yield so the cancelled task's finally-block can release user_lock
        await asyncio.sleep(0.15)

    # ── Find or create the placeholder ───────────────────────────────────────
    from app.i18n import t as _t

    last = state.get_last_bot_message(user_id)
    placeholder_message = None

    if last:
        last_msg_id, last_chat_id = last
        if last_chat_id == chat_id:
            try:
                # Edit existing bot message in-place — this is the core UX win
                _edited = await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=last_msg_id,
                    text=_t("msg.rethinking"),
                )
                placeholder_message = None if isinstance(_edited, bool) else _edited
            except (BadRequest, NetworkError) as e:
                logging.warning("edit: could not reuse message %s: %s", last_msg_id, e)

    if placeholder_message is None:
        # Fallback: send a fresh reply (e.g. old message was deleted by user)
        placeholder_message = await edited_msg.reply_text(_t("msg.rethinking"))

    state.set_last_bot_message(user_id, placeholder_message.message_id, chat_id)

    user_st = state.get_user_state(user_id)
    user_st.is_processing = True
    state.set_last_sent_message(user_id, new_text)

    done_event = asyncio.Event()
    register_heartbeat(placeholder_message.message_id, done_event, update.effective_chat)

    start_time = api_logger.log_request("telegram", method="handle_edited_message", message_type="text")

    async def edit_task_wrapper() -> None:
        try:
            async with state.get_user_lock(user_id):
                from app.handlers.agent import process_long_request

                # Inject corrected text override so agent reads the new text
                # (update.edited_message.text is the corrected version; agent reads
                #  update.message.text normally, so we patch via user_data)
                if context.user_data is not None:
                    context.user_data["_edited_text_override"] = new_text

                await process_long_request(placeholder_message, update, context)

                if context.user_data is not None:
                    context.user_data.pop("_edited_text_override", None)

                stop_heartbeat(placeholder_message.message_id)
                state.set_last_bot_message(user_id, placeholder_message.message_id, chat_id)
                logging.info("edit: completed for user %s", user_id)

                import time as _time

                elapsed = _time.time() - start_time
                api_logger.log_response("telegram", start_time, method="handle_edited_message")
                await metrics_collector.record_request("handle_edited_message", elapsed, success=True, user_id=user_id)

        except asyncio.CancelledError:
            # This edit was superseded by yet another edit — clean up silently
            logging.info("edit: task cancelled for user %s (superseded by newer edit)", user_id)
            stop_heartbeat(placeholder_message.message_id)
        except Exception as e:
            logging.error("edit: error for user %s: %s", user_id, e, exc_info=True)
            try:
                stop_heartbeat(placeholder_message.message_id)
                from app.errors import build_retry_and_roles_keyboard
                from app.i18n import t

                await placeholder_message.edit_text(t("error.generic"), reply_markup=build_retry_and_roles_keyboard())
            except Exception:
                pass

            import time as _time

            elapsed = _time.time() - start_time
            api_logger.log_response(
                "telegram",
                start_time,
                method="handle_edited_message",
                success=False,
                error_message=str(e),
            )
            await metrics_collector.record_request("handle_edited_message", elapsed, success=False, user_id=user_id)
        finally:
            usr = state.get_user_state(user_id)
            usr.is_processing = False
            state.clear_active_task(user_id)
            unregister_heartbeat(placeholder_message.message_id)
            if not done_event.is_set():
                stop_heartbeat(placeholder_message.message_id)

    from app.utils.background_tasks import submit_task

    edit_ai_task = submit_task(edit_task_wrapper())
    state.register_active_task(user_id, edit_ai_task)


def register(application: Application) -> None:
    # NEW messages only — explicit UpdateType.MESSAGE guard prevents
    # edited_message / channel_post from leaking into these handlers.
    _msg = filters.UpdateType.MESSAGE
    application.add_handler(MessageHandler(_msg & filters.TEXT & ~filters.COMMAND, handle_request, block=False))
    application.add_handler(MessageHandler(_msg & filters.PHOTO, handle_request, block=False))
    application.add_handler(MessageHandler(_msg & filters.Document.ALL, handle_request, block=False))
    application.add_handler(MessageHandler(_msg & filters.VOICE, handle_request, block=False))

    # EDITED messages — text only (photo/voice edits use new messages)
    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & filters.TEXT & ~filters.COMMAND,
            handle_edited_request,
            block=False,
        )
    )

# /app/handlers/cb_voice.py
"""Callback handlers for voice message confirmation flow.

Handles: voice:confirm, voice:edit, voice:cancel, voice:transcribe_only, voice:deep_search
Uses voice_pending data stored in context.user_data by msg_voice.py.

Enhancements:
  - **voice:deep_search**: Routes transcript through agentic research pipeline.
  - **Show & Tell**: When attached_image is present in voice_pending, injects
    TaggedImage into chat history parts for cross-modal LLM context.
"""

__all__ = ["voice_callback"]

import asyncio
import contextlib
import logging
from dataclasses import dataclass

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from app import state
from app.handlers.callbacks import (
    _HEAVY_CALLBACK_SEMAPHORE,
    _background_tasks,
)
from app.i18n import t
from app.repos.chats import get_user_chat
from app.request_context import ensure_request_id as set_request_id
from app.request_context import set_user_context


@dataclass(eq=False)
class _VoiceOperation:
    key: tuple[int, int | None, int]
    lang: str
    task: asyncio.Task | None
    pending: dict | None = None
    cancelled: bool = False


_voice_operations: dict[tuple[int, int | None, int], _VoiceOperation] = {}


def _voice_operation_key(user_id, message):
    return user_id, getattr(getattr(message, "chat", None), "id", None), message.message_id


def _register_voice_operation(user_id, message, lang, *, pending=None, operation=None):
    key = _voice_operation_key(user_id, message)
    if operation is not None:
        return operation if _voice_operation_current(operation) and operation.key == key else None
    if key in _voice_operations:
        return None
    owner = _VoiceOperation(key, lang, asyncio.current_task(), pending)
    _voice_operations[key] = owner
    if pending is not None:
        pending["_operation"] = owner
    return owner


def _voice_operation_current(operation):
    return operation is not None and not operation.cancelled and _voice_operations.get(operation.key) is operation


def _finish_voice_operation(operation):
    if _voice_operations.get(operation.key) is operation:
        _voice_operations.pop(operation.key)
    if operation.pending is not None and operation.pending.get("_operation") is operation:
        operation.pending.pop("_operation", None)


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router for all voice:* callbacks."""
    query = update.callback_query
    if query is None:
        return
    if query.from_user is None or query.message is None:
        await query.answer()
        return
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )
    data_parts = (query.data or "").split(":")
    action = data_parts[1] if len(data_parts) == 2 and data_parts[0] == "voice" else ""

    # Retrieve pending voice data bound EXACTLY to this message UI
    pending_key = f"voice_pending_{query.message.message_id}" if query.message else "voice_pending"
    pending = context.user_data.get(pending_key) if context.user_data else None
    operation = _voice_operations.get(_voice_operation_key(query.from_user.id, query.message))
    lang = pending.get("lang", "ru") if pending else operation.lang if operation else "ru"

    if pending and (
        pending.get("user_id") != query.from_user.id or pending.get("placeholder_id") != query.message.message_id
    ):
        await query.answer(t("voice.no_pending", lang))
        return

    if pending and action in {"confirm", "deep_search", "transcribe_only", "retranscribe_flash", "edit"}:
        user_lock = state.get_user_lock(pending["user_id"])
        if user_lock.locked():
            await query.answer(t("busy.toast", lang))
            return
        await user_lock.acquire()
        operation = _register_voice_operation(pending["user_id"], query.message, lang, pending=pending)
        if operation is None:
            user_lock.release()
            await query.answer(t("busy.toast", lang))
            return
        owned = True

        def release_lock(_task=None):
            nonlocal owned
            if owned:
                owned = False
                user_lock.release()

        pending["_release_lock"] = release_lock
        try:
            if not await _pending_current(pending):
                context.user_data.pop(pending_key, None)
                await query.answer()
                await query.edit_message_text(t("voice.no_pending", lang))
                return
            await _dispatch_voice_action(action, query, context, pending, pending_key, lang)
        finally:
            if operation.task is asyncio.current_task():
                _finish_voice_operation(operation)
            if pending.get("_task") is None:
                release_lock()
                pending.pop("_release_lock", None)
        return

    await _dispatch_voice_action(action, query, context, pending, pending_key, lang)


async def _pending_current(pending: dict) -> bool:
    from app.repos.memory_consent import is_private_data_snapshot_current

    operation = pending.get("_operation")
    if not _voice_operation_current(operation):
        return False
    current = await is_private_data_snapshot_current(pending["user_id"], pending.get("memory_epoch"), require_ltm=False)
    return current and _voice_operation_current(operation)


def _track_voice_task(coro, pending: dict) -> None:
    release_lock = pending["_release_lock"]
    operation = pending["_operation"]
    started = False

    async def run_owned():
        nonlocal started
        started = True
        try:
            await coro
        finally:
            release_lock()
            _finish_voice_operation(operation)

    task = asyncio.create_task(run_owned())
    operation.task = task
    pending["_task"] = task
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(release_lock)

    def clear_task(done):
        _finish_voice_operation(operation)
        if not started:
            coro.close()
        if pending.get("_task") is done:
            pending.pop("_task", None)
            pending.pop("_release_lock", None)

    task.add_done_callback(clear_task)


async def _dispatch_voice_action(action, query, context, pending, pending_key, lang):

    if action == "cancel":
        await _handle_cancel(query, context, lang)
    elif action == "transcribe_only":
        await _handle_transcribe_only(query, context, pending, lang)
    elif action == "confirm":
        await _handle_confirm(query, context, pending, lang)
    elif action == "edit":
        await _handle_edit(query, context, pending, lang)
    elif action == "deep_search":
        await _handle_deep_search(query, context, pending, lang)
    elif action == "retranscribe_flash":
        await _handle_retranscribe_flash(query, context, pending, pending_key, lang)
    else:
        await query.answer()


async def _handle_cancel(query, context, lang: str) -> None:
    """Cancel the voice request — clean up."""
    pending_key = f"voice_pending_{query.message.message_id}"
    pending = context.user_data.get(pending_key) if context.user_data is not None else None
    operation = _voice_operations.get(_voice_operation_key(query.from_user.id, query.message))
    if pending is None and operation is None:
        await query.answer()
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return
    if operation is not None:
        first_cancel = not operation.cancelled
        operation.cancelled = True
    if context.user_data is not None and context.user_data.get(pending_key) is pending:
        context.user_data.pop(pending_key, None)
    if operation is not None and operation.task is not None:
        task = operation.task
        caller_task = asyncio.current_task()
        if task is caller_task:
            await query.answer(t("voice.no_pending", lang))
            return
        if first_cancel and not task.done():
            task.cancel()
        caller_cancel = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # Owner completion can precede delivery of the caller's cancellation.
                if caller_task is not None and caller_task.cancelling():
                    caller_cancel = caller_cancel or exc
            except Exception:
                break
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        _finish_voice_operation(operation)
        if caller_cancel is not None:
            raise caller_cancel
    await query.answer()
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(t("voice.cancelled", lang))


async def _handle_transcribe_only(query, context, pending: dict | None, lang: str) -> None:
    """Show transcript without sending to AI — store in history + LTM."""
    await query.answer()

    if not pending:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    from app.handlers.msg_voice import _show_transcript_only
    from app.repos.memory_consent import private_data_lease

    # Consume before awaiting; a repeated callback cannot store this turn twice.
    context.user_data.pop(f"voice_pending_{query.message.message_id}", None)
    async with private_data_lease(
        pending["user_id"], pending["memory_epoch"], purpose="conversation:voice-transcript", require_ltm=False
    ) as current:
        if not current or not await _pending_current(pending):
            return
        chat_state = await get_user_chat(pending["user_id"])
        if not await _pending_current(pending):
            return
        await _show_transcript_only(
            query.message,
            pending["transcript"],
            lang,
            pending["user_id"],
            pending.get("voice_bytes", b""),
            type("FakeVoice", (), {"file_unique_id": pending.get("file_unique_id")})(),
            chat_state=chat_state,
        )

    # Clean up
    if context.user_data and query.message:
        context.user_data.pop(f"voice_pending_{query.message.message_id}", None)


async def _handle_confirm(query, context, pending: dict | None, lang: str) -> None:
    """Route transcript through the AI chat pipeline as a user message.

    If ``attached_image`` is present in pending (Show & Tell), injects the image
    as a TaggedImage into the chat history so the LLM sees both voice + photo.
    """
    if not pending:
        await query.answer()
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    user_id = pending["user_id"]
    await query.answer()

    # Update placeholder to show processing, leaving transcript intact
    from app.utils.formatting import TelegramFormatter

    final_text = f"{t('voice.transcript_label', lang)}\n\n{pending['transcript']}\n\n✅ _(Принято)_"
    fmt, pm = TelegramFormatter.format_text(final_text)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(fmt, parse_mode=pm, reply_markup=None)

    new_placeholder = await query.message.reply_text("⏳ _Анализирую текст..._", parse_mode="Markdown")
    if context.user_data.get(f"voice_pending_{query.message.message_id}") is not pending:
        await new_placeholder.edit_text(t("voice.cancelled", lang))
        return
    transcript = pending["transcript"]
    attached_image = pending.get("attached_image")

    # Clean up pending BEFORE starting the task
    if context.user_data and query.message:
        context.user_data.pop(f"voice_pending_{query.message.message_id}", None)

    async def _confirm_wrapper() -> None:
        try:
            from app.handlers.ai_chat import _handle_regular_chat

            async with _HEAVY_CALLBACK_SEMAPHORE:
                if not await _pending_current(pending):
                    return
                chat_state = await get_user_chat(user_id)
                if not await _pending_current(pending):
                    return

                # Keep multimodal content in the current canonical user turn.
                # Pre-appending it to history would make the chat pipeline add
                # the same transcript a second time.
                from app.i18n import t as _t

                parts: list = [f"{_t('voice.history_marker', lang)}\n{transcript}"]

                if attached_image:
                    from app.utils.image_utils import TaggedImage

                    parts.append(
                        TaggedImage(
                            data=attached_image["bytes"],
                            cache_key=attached_image.get("file_unique_id"),
                            task_type="default",
                            pre_compressed=True,
                        )
                    )
                    logging.info(
                        "Show & Tell: injected image %s into voice confirm for user %s",
                        attached_image.get("file_unique_id"),
                        user_id,
                    )

                # Voice-for-Voice: source is a voice message, but we ONLY reply
                # with voice if the user specifically asked for it (parsed intent).
                # This flag is per-request and NOT persisted.
                reply_with_voice = pending.get("reply_with_voice", False)
                await _handle_regular_chat(
                    new_placeholder,  # type: ignore[arg-type]
                    user_id,
                    transcript,
                    chat_state,
                    reply_with_voice=reply_with_voice,
                    user_parts=parts,
                )
        except Exception as e:
            logging.error("voice:confirm task failed: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await new_placeholder.edit_text(t("error.generic", lang))

    _track_voice_task(_confirm_wrapper(), pending)


async def _handle_edit(query, context, pending: dict | None, lang: str) -> None:
    """Ask user to type corrected text; reply with original as reference."""
    await query.answer()

    if not pending:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    transcript = pending["transcript"]

    # Show original transcript as reference + prompt for corrected text
    from app.utils.formatting import TelegramFormatter

    edit_text = f"{t('voice.edit_original', lang)}\n\n_{transcript}_\n\n{t('voice.edit_prompt', lang)}"
    formatted, parse_mode = TelegramFormatter.format_text(edit_text)

    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(formatted, parse_mode=parse_mode, reply_markup=None)

    # Mark that we're waiting for an edited text from the user
    if context.user_data and _voice_operation_current(pending.get("_operation")):
        context.user_data["voice_edit_pending"] = True
        # Keep voice_pending so we can reference language/user_id later


async def _handle_deep_search(query, context, pending: dict | None, lang: str) -> None:
    """Route voice transcript through the agentic research pipeline (Deep Search).

    This is triggered by the "🔍 Deep Search" button, which appears when ASR
    detects INTENT:SEARCH in the voice message.
    """
    if not pending:
        await query.answer()
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    user_id = pending["user_id"]
    await query.answer()

    # Update placeholder to finalize transcript
    from app.utils.formatting import TelegramFormatter

    final_text = f"{t('voice.transcript_label', lang)}\n\n{pending['transcript']}\n\n🔍 _Deep Search_"
    fmt, pm = TelegramFormatter.format_text(final_text)
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(fmt, parse_mode=pm, reply_markup=None)

    new_placeholder = await query.message.reply_text("⏳ _Анализирую текст..._", parse_mode="Markdown")
    if context.user_data.get(f"voice_pending_{query.message.message_id}") is not pending:
        await new_placeholder.edit_text(t("voice.cancelled", lang))
        return
    transcript = pending["transcript"]

    # Clean up pending
    if context.user_data and query.message:
        context.user_data.pop(f"voice_pending_{query.message.message_id}", None)

    async def _deep_search_wrapper() -> None:
        try:
            from app.handlers.ai_search import _handle_research_agent

            async with _HEAVY_CALLBACK_SEMAPHORE:
                if not await _pending_current(pending):
                    return
                chat_state = await get_user_chat(user_id)
                if not await _pending_current(pending):
                    return

                # Store only after admission and exact generation validation.
                from app.i18n import t as _t

                chat_state.history.append(
                    {
                        "role": "user",
                        "parts": [f"🔍 {_t('voice.history_marker', lang)}\n{transcript}"],
                    }
                )
                await _handle_research_agent(
                    new_placeholder,
                    user_id,
                    transcript,
                    chat_state,
                )
        except Exception as e:
            logging.error("voice:deep_search task failed: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await new_placeholder.edit_text(t("error.generic", lang))

    _track_voice_task(_deep_search_wrapper(), pending)


async def _handle_retranscribe_flash(query, context, pending: dict | None, pending_key: str, lang: str) -> None:
    """Re-transcribe using the smarter Gemini Flash model."""
    if not pending:
        await query.answer()
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    user_id = pending["user_id"]
    await query.answer()

    # Update UI to show processing
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text("⚡ _Идёт повторная транскрибация..._")

    placeholder_message = query.message
    voice_bytes = pending["voice_bytes"]

    # We DO NOT pop the pending data here, because we want to update it!

    async def _retranscribe_wrapper() -> None:
        try:
            from app.handlers.msg_voice import _show_confirmation_ui
            from app.repos.memory_consent import private_data_lease
            from app.utils.multimodal_processor import transcribe_voice

            async with (
                _HEAVY_CALLBACK_SEMAPHORE,
                private_data_lease(
                    user_id, pending["memory_epoch"], purpose="conversation:voice-retranscribe", require_ltm=False
                ) as current,
            ):
                if (
                    not current
                    or not await _pending_current(pending)
                    or context.user_data.get(pending_key) is not pending
                ):
                    context.user_data.pop(pending_key, None)
                    return
                # Retranscribe with the specific premium model requested
                new_transcript, new_intent, new_draw_prompt = await transcribe_voice(
                    voice_bytes, model="gemini-3.5-flash"
                )
                if not await _pending_current(pending) or context.user_data.get(pending_key) is not pending:
                    context.user_data.pop(pending_key, None)
                    return

                if new_transcript is None:
                    # Revert nicely
                    await query.edit_message_text("❌ _Не удалось перерасшифровать. Попробуйте снова или отмените._")
                    return

                # We need to update the transcript in the context so the user can Confirm the NEW transcript
                from app.voice_intent import detect_tts_intent

                voice_decision = await detect_tts_intent(user_text=new_transcript)
                if not await _pending_current(pending) or context.user_data.get(pending_key) is not pending:
                    context.user_data.pop(pending_key, None)
                    return
                pending["transcript"] = new_transcript
                pending["intent"] = new_intent
                pending["draw_prompt"] = new_draw_prompt
                pending["reply_with_voice"] = voice_decision.explicit_tts
                if context.user_data:
                    context.user_data[pending_key] = pending

                class MockVoice:
                    file_unique_id = pending.get("file_unique_id")

                await _show_confirmation_ui(
                    placeholder=placeholder_message,
                    transcript=new_transcript,
                    lang=lang,
                    user_id=user_id,
                    voice_bytes=voice_bytes,
                    voice=MockVoice(),
                    context=context,
                    intent=new_intent,
                    attached_image=pending.get("attached_image"),
                    memory_epoch=pending["memory_epoch"],
                    _operation=pending["_operation"],
                )

        except Exception as e:
            logging.error("voice:retranscribe_flash task failed: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await placeholder_message.edit_text(t("error.generic", lang))

    _track_voice_task(_retranscribe_wrapper(), pending)

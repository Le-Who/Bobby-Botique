# /app/handlers/cb_voice.py
"""Callback handlers for voice message confirmation flow.

Handles: voice:confirm, voice:edit, voice:cancel, voice:transcribe_only
Uses voice_pending data stored in context.user_data by msg_voice.py.
"""

__all__ = ["voice_callback"]

import asyncio
import logging

import contextlib

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
from app.request_context import set_request_id, set_user_context


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Router for all voice:* callbacks."""
    query = update.callback_query
    set_request_id(f"tgcb-{query.from_user.id}-{query.id}")
    set_user_context(
        query.from_user.id,
        getattr(query.message.chat, "id", None) if query.message else None,
    )

    action = query.data.split(":")[1] if ":" in query.data else ""

    # Retrieve pending voice data
    pending = context.user_data.get("voice_pending") if context.user_data else None
    lang = pending.get("lang", "ru") if pending else "ru"

    if action == "cancel":
        await _handle_cancel(query, context, lang)
    elif action == "transcribe_only":
        await _handle_transcribe_only(query, context, pending, lang)
    elif action == "confirm":
        await _handle_confirm(query, context, pending, lang)
    elif action == "edit":
        await _handle_edit(query, context, pending, lang)
    else:
        await query.answer()


async def _handle_cancel(query, context, lang: str) -> None:
    """Cancel the voice request — clean up."""
    await query.answer()
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(t("voice.cancelled", lang))
    # Clean up pending data
    if context.user_data:
        context.user_data.pop("voice_pending", None)


async def _handle_transcribe_only(query, context, pending: dict | None, lang: str) -> None:
    """Show transcript without sending to AI — store in history + LTM."""
    await query.answer()

    if not pending:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    from app.handlers.msg_voice import _show_transcript_only

    await _show_transcript_only(
        query.message,
        pending["transcript"],
        lang,
        pending["user_id"],
        pending.get("voice_bytes", b""),
        type("FakeVoice", (), {"file_unique_id": pending.get("file_unique_id")})(),
    )

    # Clean up
    if context.user_data:
        context.user_data.pop("voice_pending", None)


async def _handle_confirm(query, context, pending: dict | None, lang: str) -> None:
    """Route transcript through the AI chat pipeline as a user message."""
    if not pending:
        await query.answer()
        with contextlib.suppress(telegram.error.BadRequest):
            await query.edit_message_text(t("voice.no_pending", lang))
        return

    user_id = pending["user_id"]
    user_lock = state.get_user_lock(user_id)

    # Single query.answer() — busy check
    await query.answer(t("busy.toast", lang) if user_lock.locked() else "")
    if user_lock.locked():
        return

    # Update placeholder to show processing
    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(t("voice.sending_request", lang))

    placeholder_message = query.message
    transcript = pending["transcript"]

    # Clean up pending BEFORE starting the task
    if context.user_data:
        context.user_data.pop("voice_pending", None)

    async def _confirm_wrapper() -> None:
        try:
            from app.handlers.ai_chat import _handle_regular_chat

            chat_state = await get_user_chat(user_id)

            # Store voice transcript in history as user message
            from app.i18n import t as _t

            chat_state.history.append(
                {
                    "role": "user",
                    "parts": [f"{_t('voice.history_marker', lang)}\n{transcript}"],
                }
            )

            async with _HEAVY_CALLBACK_SEMAPHORE, user_lock:
                await _handle_regular_chat(
                    placeholder_message,  # type: ignore[arg-type]
                    user_id,
                    transcript,
                    chat_state,
                )
        except Exception as e:
            logging.error("voice:confirm task failed: %s", e, exc_info=True)
            with contextlib.suppress(Exception):
                await placeholder_message.edit_text(t("error.generic", lang))

    _task = asyncio.create_task(_confirm_wrapper())
    _background_tasks.add(_task)
    _task.add_done_callback(_background_tasks.discard)


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

    edit_text = (
        f"{t('voice.edit_original', lang)}\n\n"
        f"_{transcript}_\n\n"
        f"{t('voice.edit_prompt', lang)}"
    )
    formatted, parse_mode = TelegramFormatter.format_text(edit_text)

    with contextlib.suppress(telegram.error.BadRequest):
        await query.edit_message_text(formatted, parse_mode=parse_mode, reply_markup=None)

    # Mark that we're waiting for an edited text from the user
    if context.user_data:
        context.user_data["voice_edit_pending"] = True
        # Keep voice_pending so we can reference language/user_id later

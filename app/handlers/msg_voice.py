# /app/handlers/msg_voice.py
"""Voice message handler — conversational voice flow.

Downloads OGG from Telegram, transcribes via Gemini, shows transcript
with inline confirmation buttons (confirm / edit / transcribe-only / cancel).
On confirmation, routes through the AI chat pipeline for a full response.

Called from ``handle_request`` in messages.py via task_wrapper (inherits all
auth/rate-limit/tracing/lock/heartbeat guards), NOT registered standalone.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from app.i18n import detect_language, t
from app.repos.chats import get_user_chat, update_user_chat
from app.utils.background_tasks import submit_retryable

# Overall timeout for the entire voice processing pipeline (download + transcribe + UI).
# Safety net above the per-call resilience timeouts (30s × 2 retries × 3 keys = 180s max).
_VOICE_OVERALL_TIMEOUT_S = 90.0


async def handle_voice_inline(
    placeholder_message: Message,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle voice message — processes voice within caller-provided placeholder.

    The caller (messages.py task_wrapper) is responsible for:
      - Creating the placeholder message
      - Acquiring user_lock
      - Registering heartbeat
      - Logging RESPONSE COMPLETED
      - Error recovery UI on exception

    This function raises on failure (caller handles the exception).
    """
    user_id = update.effective_user.id
    voice = update.message.voice

    if not voice:
        return

    lang = "ru"  # default; refined after transcription

    # Wrap entire voice flow in an overall timeout safety net
    try:
        await asyncio.wait_for(
            _process_voice_pipeline(placeholder_message, update, context, user_id, voice, lang),
            timeout=_VOICE_OVERALL_TIMEOUT_S,
        )
    except TimeoutError:
        logging.error(
            "Voice processing timed out after %.0fs for user %s",
            _VOICE_OVERALL_TIMEOUT_S, user_id,
        )
        await placeholder_message.edit_text(t("voice.error", lang))
        raise


async def _process_voice_pipeline(
    placeholder: Message,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    voice,
    lang: str,
) -> None:
    """Core voice pipeline: download → transcribe → show UI."""
    # 1. Download OGG bytes from Telegram
    voice_file = await voice.get_file()
    voice_bytes = bytes(await voice_file.download_as_bytearray())

    if not voice_bytes:
        await placeholder.edit_text(t("voice.download_failed", lang))
        return

    # 2. Transcribe via multimodal processor (with retries + key fallback)
    from app.utils.multimodal_processor import transcribe_voice

    transcript, intent = await transcribe_voice(voice_bytes, mime_type="audio/ogg")

    if not transcript:
        await placeholder.edit_text(t("voice.transcription_failed", lang))
        return

    # Refine language detection from actual transcript content
    lang = detect_language(transcript)

    # 3. Show transcript with confirmation buttons
    from app.utils.formatting import TelegramFormatter

    if intent == "transcription":
        # Intent: user asked for transcription → show clean transcript
        await _show_transcript_only(
            placeholder, transcript, lang,
            user_id, voice_bytes, voice, chat_state=None,
        )
    else:
        # Intent: conversational → show with confirmation buttons
        confirm_text = (
            f"{t('voice.transcript_label', lang)}\n\n"
            f"{transcript}\n\n"
            f"_{t('voice.confirm_prompt', lang)}_"
        )
        formatted, parse_mode = TelegramFormatter.format_text(confirm_text)
        keyboard = [
            [InlineKeyboardButton(t("voice.btn_confirm", lang), callback_data="voice:confirm")],
            [
                InlineKeyboardButton(t("voice.btn_transcribe_only", lang), callback_data="voice:transcribe_only"),
                InlineKeyboardButton(t("voice.btn_edit", lang), callback_data="voice:edit"),
            ],
            [InlineKeyboardButton(t("voice.btn_cancel", lang), callback_data="voice:cancel")],
        ]
        await placeholder.edit_text(
            formatted,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        # Store pending voice data for callback handler
        if context.user_data is not None:
            context.user_data["voice_pending"] = {
                "transcript": transcript,
                "voice_bytes": voice_bytes,
                "user_id": user_id,
                "lang": lang,
                "file_unique_id": voice.file_unique_id,
                "placeholder_id": placeholder.message_id,
            }

    logging.info(
        "Voice message processed for user %s: %d bytes, %ds duration, intent=%s",
        user_id,
        len(voice_bytes),
        voice.duration,
        intent,
    )


async def _show_transcript_only(
    placeholder,
    transcript: str,
    lang: str,
    user_id: int,
    voice_bytes: bytes,
    voice,
    *,
    chat_state=None,
) -> None:
    """Show clean transcript (no buttons), store in history + LTM.

    Used for both explicit transcription intent and the "transcribe_only" callback.
    """
    from app.utils.formatting import TelegramFormatter

    display_text = f"{t('voice.transcript_label', lang)}\n\n{transcript}"
    formatted, parse_mode = TelegramFormatter.format_text(display_text)
    await placeholder.edit_text(formatted, parse_mode=parse_mode, reply_markup=None)

    # Store in chat history
    if chat_state is None:
        chat_state = await get_user_chat(user_id)

    chat_state.history.append(
        {
            "role": "user",
            "parts": [f"{t('voice.history_marker', lang)}\n{transcript}"],
        }
    )
    await update_user_chat(user_id, chat_state)

    # Store in long-term memory (background, non-blocking)
    if chat_state.ltm_enabled:
        _voice_uid = user_id
        _voice_file_id = getattr(voice, "file_unique_id", None)
        _voice_bytes = voice_bytes

        def _bg_voice_ltm():
            async def _store():
                from app.utils.multimodal_processor import process_media_for_memory

                await process_media_for_memory(
                    _voice_bytes,
                    _voice_uid,
                    media_type="voice",
                    telegram_file_id=_voice_file_id,
                )

            return _store()

        submit_retryable(_bg_voice_ltm, retry=2)

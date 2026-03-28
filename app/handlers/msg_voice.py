# /app/handlers/msg_voice.py
"""Voice message handler — conversational voice flow.

Downloads OGG from Telegram, transcribes via Gemini, shows transcript
with inline confirmation buttons (confirm / edit / transcribe-only / cancel).

Smart enhancements:
  - **Auto-Routing**: If thinking_classifier deems the transcript LOW complexity,
    the confirmation UI is bypassed and the request is sent directly to AI chat.
  - **Agentic Search**: When ASR detects INTENT:SEARCH, the primary button
    becomes "🔍 Deep Search" routing to the agentic research pipeline.
  - **Show & Tell**: If the voice is a Reply to a photo message, the image
    bytes are attached to voice_pending for cross-modal LLM context.

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
            _VOICE_OVERALL_TIMEOUT_S,
            user_id,
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
    """Core voice pipeline: download → transcribe → show UI / auto-route."""
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

    # 3. Detect "Show & Tell" — voice is a Reply to a photo message
    attached_image = await _detect_show_and_tell(update)

    # 4. Route based on intent
    if intent == "transcription":
        # Intent: user asked for transcription → show clean transcript
        await _show_transcript_only(
            placeholder,
            transcript,
            lang,
            user_id,
            voice_bytes,
            voice,
            chat_state=None,
        )
    elif intent == "search":
        # Intent: user wants to search the web → show with Deep Search button
        await _show_confirmation_ui(
            placeholder,
            transcript,
            lang,
            user_id,
            voice_bytes,
            voice,
            context,
            intent=intent,
            attached_image=attached_image,
        )
    else:
        # Intent: conversational → check if auto-routing is applicable
        should_auto = _should_auto_route(transcript)

        if should_auto and not attached_image:
            # Low complexity + no attached media → auto-submit
            logging.info("Voice auto-route triggered for user %s (LOW complexity)", user_id)
            await _auto_route_to_chat(
                placeholder,
                transcript,
                lang,
                user_id,
                voice_bytes,
                voice,
                context,
            )
        else:
            # Show confirmation UI with standard buttons
            await _show_confirmation_ui(
                placeholder,
                transcript,
                lang,
                user_id,
                voice_bytes,
                voice,
                context,
                intent=intent,
                attached_image=attached_image,
            )

    logging.info(
        "Voice message processed for user %s: %d bytes, %ds duration, intent=%s, show_tell=%s",
        user_id,
        len(voice_bytes),
        voice.duration,
        intent,
        bool(attached_image),
    )


def _should_auto_route(transcript: str) -> bool:
    """Determine if the transcript is simple enough to bypass confirmation UI.

    Uses lexical heuristics and the thinking_classifier (0ms, no API calls).
    Returns True for explicit intent commands or LOW complexity messages.
    """
    text_lower = transcript.strip().lower()

    # Heuristic 1: Explicit voice commands (> 10 chars to avoid false positives)
    # Give some leeway for ASR padding/filler words at the beginning
    import re

    action_pattern = re.compile(r"^(?:вот,?\s*)?(сочини|напиши|бот,?|расскажи|сделай)\s", re.IGNORECASE)
    if action_pattern.match(text_lower) and len(text_lower) > 10:
        return True

    # Heuristic 2: Low-complexity classifier (greetings, confirmations)
    from app.thinking_classifier import classify_thinking_level

    level = classify_thinking_level(transcript)
    return level == "low"


async def _detect_show_and_tell(update: Update) -> dict | None:
    """Check if the voice message is a Reply to a photo and extract image bytes.

    Returns a dict with 'bytes', 'mime_type', 'file_unique_id' if photo found,
    or None otherwise.
    """
    reply = update.message.reply_to_message
    if not reply or not reply.photo:
        return None

    try:
        # Use the largest available photo resolution
        photo = reply.photo[-1]
        file_unique_id = photo.file_unique_id

        # Check the compressed image cache first (3 min TTL, covers recent photos)
        from app.utils.image_utils import _compressed_cache

        cached = _compressed_cache.get(file_unique_id)
        if cached:
            logging.info("Show & Tell: image %s found in compressed cache", file_unique_id)
            return {
                "bytes": cached,
                "mime_type": "image/jpeg",
                "file_unique_id": file_unique_id,
            }

        # Cache miss — download from Telegram (fast fallback)
        photo_file = await photo.get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())
        if photo_bytes:
            logging.info("Show & Tell: downloaded image %s (%d bytes)", file_unique_id, len(photo_bytes))
            return {
                "bytes": photo_bytes,
                "mime_type": "image/jpeg",
                "file_unique_id": file_unique_id,
            }
    except Exception as e:
        logging.warning("Show & Tell: failed to get reply photo: %s", e)

    return None


async def _show_confirmation_ui(
    placeholder: Message,
    transcript: str,
    lang: str,
    user_id: int,
    voice_bytes: bytes,
    voice,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    intent: str = "conversational",
    attached_image: dict | None = None,
) -> None:
    """Show transcript with confirmation buttons. Dynamic layout based on intent."""
    from app.utils.formatting import TelegramFormatter

    # Build display text
    show_tell_indicator = f"\n🖼️ _{t('voice.show_tell_detected', lang)}_" if attached_image else ""
    confirm_text = (
        f"{t('voice.transcript_label', lang)}\n\n"
        f"{transcript}\n\n"
        f"_{t('voice.confirm_prompt', lang)}_"
        f"{show_tell_indicator}"
    )
    formatted, parse_mode = TelegramFormatter.format_text(confirm_text)

    # Build keyboard based on intent
    if intent == "search":
        # Primary action = Deep Search
        keyboard = [
            [InlineKeyboardButton(t("voice.btn_deep_search", lang), callback_data="voice:deep_search")],
            [
                InlineKeyboardButton(t("voice.btn_confirm", lang), callback_data="voice:confirm"),
                InlineKeyboardButton(t("voice.btn_transcribe_only", lang), callback_data="voice:transcribe_only"),
            ],
            [InlineKeyboardButton("⚡ Re-transcribe (Flash)", callback_data="voice:retranscribe_flash")],
            [InlineKeyboardButton(t("voice.btn_cancel", lang), callback_data="voice:cancel")],
        ]
    else:
        keyboard = [
            [InlineKeyboardButton(t("voice.btn_confirm", lang), callback_data="voice:confirm")],
            [
                InlineKeyboardButton(t("voice.btn_transcribe_only", lang), callback_data="voice:transcribe_only"),
                InlineKeyboardButton(t("voice.btn_edit", lang), callback_data="voice:edit"),
            ],
            [InlineKeyboardButton("⚡ Re-transcribe (Flash)", callback_data="voice:retranscribe_flash")],
            [InlineKeyboardButton(t("voice.btn_cancel", lang), callback_data="voice:cancel")],
        ]

    await placeholder.edit_text(
        formatted,
        parse_mode=parse_mode,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    # Store pending voice data for callback handler
    if context.user_data is not None:
        pending = {
            "transcript": transcript,
            "voice_bytes": voice_bytes,
            "user_id": user_id,
            "lang": lang,
            "file_unique_id": voice.file_unique_id,
            "placeholder_id": placeholder.message_id,
            "intent": intent,
            "reply_with_voice": "озвучь ответ" in transcript.lower()
            or "ответь голосом" in transcript.lower()
            or "прочитай вслух" in transcript.lower(),
        }
        # Attach "Show & Tell" image if present
        if attached_image:
            pending["attached_image"] = attached_image

        context.user_data[f"voice_pending_{placeholder.message_id}"] = pending


async def _auto_route_to_chat(
    placeholder: Message,
    transcript: str,
    lang: str,
    user_id: int,
    voice_bytes: bytes,
    voice,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Auto-route low-complexity voice to AI chat, skipping confirmation UI.

    Shows a brief indicator that auto-routing happened, then runs the
    regular chat pipeline using a NEW placeholder message so the transcript
    remains visible in chat history.
    """
    from app.utils.formatting import TelegramFormatter

    auto_text = f"{t('voice.transcript_label', lang)}\n\n{transcript}\n\n⚡ _{t('voice.auto_confirm', lang)}_"
    formatted, parse_mode = TelegramFormatter.format_text(auto_text)
    await placeholder.edit_text(formatted, parse_mode=parse_mode, reply_markup=None)

    # Spawn a new placeholder for the LLM response so the transcript isn't overwritten
    new_placeholder = await placeholder.reply_text("⏳ _Анализирую текст..._", parse_mode="Markdown")

    # Store in history
    chat_state = await get_user_chat(user_id)
    chat_state.history.append(
        {
            "role": "user",
            "parts": [f"{t('voice.history_marker', lang)}\n{transcript}"],
        }
    )

    # Run the chat pipeline inline (we already hold user_lock from caller)
    from app.handlers.ai_chat import _handle_regular_chat

    # Dynamically resolve if the user requested voice readout
    reply_with_voice = (
        "озвучь ответ" in transcript.lower()
        or "ответь голосом" in transcript.lower()
        or "прочитай вслух" in transcript.lower()
    )
    await _handle_regular_chat(new_placeholder, user_id, transcript, chat_state, reply_with_voice=reply_with_voice)

    # Background LTM storage
    if chat_state.ltm_enabled:
        _uid = user_id
        _fid = getattr(voice, "file_unique_id", None)
        _vb = voice_bytes

        def _bg():
            async def _store():
                from app.utils.multimodal_processor import process_media_for_memory

                await process_media_for_memory(_vb, _uid, media_type="voice", telegram_file_id=_fid)

            return _store()

        submit_retryable(_bg, retry=2)


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

# /app/handlers/msg_voice.py
"""Voice message handler — downloads OGG from Telegram, transcribes via
gemini-3.1-flash-lite with high thinking, saves transcript to chat history
and stores in long-term memory.

Called from ``handle_request`` in messages.py (inherits all auth/rate-limit/
tracing guards), NOT registered as a standalone handler.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.repos.chats import get_user_chat, update_user_chat
from app.utils.background_tasks import submit_retryable


async def handle_voice_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice message — called from inside handle_request (already authed).

    Auth, rate-limits, tracing, and metrics are handled by the caller.
    """
    user_id = update.effective_user.id
    voice = update.message.voice

    if not voice:
        return

    # Duration guard (skip very short accidental recordings)
    # voice.duration may be int or timedelta depending on PTB version
    if int(voice.duration) < 1:
        await update.message.reply_text("⚠️ Голосовое сообщение слишком короткое.")
        return

    placeholder = await update.message.reply_text("🎙️ Расшифровываю голосовое сообщение...")

    try:
        # 1. Download OGG bytes from Telegram
        voice_file = await voice.get_file()
        voice_bytes = bytes(await voice_file.download_as_bytearray())

        if not voice_bytes:
            await placeholder.edit_text("❌ Не удалось загрузить голосовое сообщение.")
            return

        # 2. Transcribe via multimodal processor (with retries + key fallback)
        from app.utils.multimodal_processor import transcribe_voice

        transcript = await transcribe_voice(voice_bytes, mime_type="audio/ogg")

        if not transcript:
            await placeholder.edit_text("❌ Не удалось расшифровать голосовое сообщение. Попробуйте ещё раз.")
            return

        # 3. Show transcript to user
        from app.utils.formatting import TelegramFormatter

        display_text = f"🎙️ *Расшифровка:*\n\n{transcript}"
        formatted, parse_mode = TelegramFormatter.format_text(display_text)
        await placeholder.edit_text(formatted, parse_mode=parse_mode)

        # 4. Store transcript in chat history (like a regular user message)
        chat_state = await get_user_chat(user_id)
        chat_state.history.append(
            {
                "role": "user",
                "parts": [f"[Голосовое сообщение]\n{transcript}"],
            }
        )
        await update_user_chat(user_id, chat_state)

        # 5. Store in long-term memory (background, non-blocking)
        if chat_state.ltm_enabled:
            _voice_uid = user_id
            _voice_file_id = voice.file_unique_id

            def _bg_voice_ltm():
                async def _store():
                    from app.utils.multimodal_processor import process_media_for_memory

                    await process_media_for_memory(
                        voice_bytes,
                        _voice_uid,
                        media_type="voice",
                        telegram_file_id=_voice_file_id,
                    )

                return _store()

            submit_retryable(_bg_voice_ltm, retry=2)

        logging.info(
            "Voice message processed for user %s: %d bytes, %ds duration",
            user_id,
            len(voice_bytes),
            voice.duration,
        )

    except Exception as e:
        logging.error("Error processing voice message for user %s: %s", user_id, e, exc_info=True)
        try:
            await placeholder.edit_text("❌ Произошла ошибка при обработке голосового сообщения.")
        except Exception as edit_err:
            logging.error("Could not edit placeholder: %s", edit_err)

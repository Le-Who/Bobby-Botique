# /app/voice_engine.py
"""Voice Engine 3.0 — orchestrates TTS generation and Telegram voice delivery.

Provides a single entry point for the chat handler to fire-and-forget
voice reply generation after text streaming completes.

Strategy: text → byte-based sentence chunking (≤3500 UTF-8 bytes/chunk) →
           sequential REST TTS calls → PCM concatenation → ffmpeg →
           OGG Opus → bot.send_voice()

Sequential (not parallel) TTS calls are mandatory: Free Tier keys have a
daily RPD cap of ~10 requests; burst parallel calls exhaust the cap and
trigger day-long suspensions. Sequential processing guarantees each call
uses a different (rotated) key slot.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from telegram import Bot

from app.utils.background_tasks import submit_retryable


async def _generate_single_chunk(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
    timeout: float = 120.0,
) -> bytes | None:
    """Generate PCM audio for a single text chunk with key rotation.

    Args:
        text_chunk: Pre-cleaned text fragment to synthesise.
        voice: Prebuilt Gemini TTS voice name.
        failed_keys: Shared set of key hashes to skip (mutated in place).
        timeout: HTTP timeout forwarded to generate_speech; callers should
            pass an adaptive value computed from the full text length.

    Returns raw PCM 24kHz 16-bit mono bytes, or None on failure.
    """
    from app.errors import classify_key_error
    from app.handlers.ai_core import _resolve_ai_request
    from app.providers.tts import generate_speech
    from app.repos.keys import get_key_status_manager

    status_mgr = get_key_status_manager()

    for attempt in range(3):
        key_data, model_used, _ = await _resolve_ai_request(
            "gemini-2.5-flash-preview-tts", excluded_key_hashes=failed_keys
        )
        if not key_data:
            break

        try:
            pcm = await generate_speech(text_chunk, key_data["api_key"], voice=voice, timeout=timeout)
            if pcm:
                return pcm
            else:
                raise ValueError("TTS provider returned empty audio buffer")
        except Exception as e:
            err_str = str(e)
            logging.warning(
                "TTS chunk failed (attempt %d/3, %d chars): %s",
                attempt + 1,
                len(text_chunk),
                err_str,
            )
            failed_keys.add(key_data["key_hash"])
            try:
                err_cat = classify_key_error(err_str)
                await status_mgr.suspend_key(key_data["key_hash"], model_used, err_cat, err_str[:200])
            except Exception:
                pass

    return None


async def _generate_and_send_voice(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    response_text: str,
    *,
    voice: str = "Aoede",
) -> None:
    """Generate TTS audio and send as Telegram voice message.

    Pipeline:
      1. Clean text and split at sentence boundaries (≤3500 UTF-8 bytes/chunk)
      2. Compute adaptive timeout (30–120 s) proportional to text length
      3. Generate PCM for each chunk sequentially (shared key rotation)
      4. Concatenate raw PCM buffers (same 24kHz 16-bit mono format)
      5. Transcode PCM → OGG Opus
      6. Send voice message

    Failure policy:
      - First chunk failure → abort (no audio without the opening context)
      - Later chunk failures → send the portion already generated
      - Complete failure → edit status message to inform the user

    Memory-critical: aggressively deletes intermediate buffers at each pipeline
    stage to avoid OOM on the 500MB RAM budget.
    """
    from app.providers.tts import _chunk_text_by_sentences, _clean_text_for_speech
    from app.utils.audio import make_voice_file, pcm_to_ogg_opus

    # Show recording indicator message to user
    status_msg = None
    try:
        status_msg = await bot.send_message(
            chat_id,
            "🎙️ _Синтезирую голос..._",
            reply_to_message_id=reply_to_message_id,
            parse_mode="Markdown",
            disable_notification=True,
        )
    except Exception:
        pass

    try:
        # 1. Clean & chunk using byte-based boundaries
        # 3500 bytes ≤ Cloud TTS 4000-byte text-field limit; safe for Cyrillic (2 bytes/char).
        clean_text = _clean_text_for_speech(response_text)
        if not clean_text:
            logging.warning("Voice reply: cleaned text is empty (chat_id=%s)", chat_id)
            return

        chunks = _chunk_text_by_sentences(clean_text, max_bytes=3500)
        logging.info(
            "Voice reply: %d chars / %d bytes → %d chunk(s) for TTS",
            len(clean_text),
            len(clean_text.encode("utf-8")),
            len(chunks),
        )

        # 2. Adaptive timeout: ~1 s per 60 chars + 15 s base; capped at [30, 120].
        # Prevents long hangs for short messages while covering large chunks.
        adaptive_timeout = min(120.0, max(30.0, len(clean_text) / 60.0 + 15.0))

        # 3. Generate PCM for each chunk sequentially.
        # Sequential processing is MANDATORY: Free Tier keys have a ~10-RPD cap;
        # burst parallel calls exhaust it and trigger day-long suspensions.
        failed_keys: set[str] = set()
        pcm_parts: list[bytes] = []

        for i, chunk in enumerate(chunks):
            pcm = await _generate_single_chunk(chunk, voice, failed_keys, timeout=adaptive_timeout)
            if pcm:
                pcm_parts.append(pcm)
            else:
                if i == 0:
                    # First chunk failed — no audio without the opening context.
                    logging.warning("Voice reply: first chunk failed, aborting (chat_id=%s)", chat_id)
                else:
                    # Later chunk failed — still deliver what was generated.
                    logging.warning(
                        "Voice reply: chunk %d/%d failed, sending partial audio (chat_id=%s)",
                        i + 1,
                        len(chunks),
                        chat_id,
                    )
                break

        if not pcm_parts:
            logging.warning("No audio generated for voice reply (chat_id=%s)", chat_id)
            # Inform user instead of silently deleting the status indicator.
            if status_msg:
                try:
                    await status_msg.edit_text(
                        "🔇 _Голосовой ответ недоступен — превышена квота API._",
                        parse_mode="Markdown",
                    )
                    await asyncio.sleep(5.0)
                except Exception:
                    pass
            return

        if len(pcm_parts) < len(chunks):
            logging.warning(
                "Voice reply: %d/%d chunks succeeded, voicing partial text (chat_id=%s)",
                len(pcm_parts),
                len(chunks),
                chat_id,
            )

        pcm_audio = b"".join(pcm_parts)
        del pcm_parts  # Free list references

        logging.info("Voice reply: TTS audio generated (%d bytes PCM)", len(pcm_audio))

        # 3. Transcode PCM → OGG Opus
        ogg_bytes = await pcm_to_ogg_opus(pcm_audio)
        del pcm_audio  # ⚠ Free PCM immediately — OGG is 10-20x smaller

        if ogg_bytes is None:
            logging.error("PCM→OGG transcoding failed for voice reply")
            return

        ogg_size = len(ogg_bytes)

        # 4. Send voice message
        try:
            voice_file = make_voice_file(ogg_bytes)
            del ogg_bytes  # BytesIO holds its own copy, free original

            await bot.send_voice(
                chat_id=chat_id,
                voice=voice_file,
                reply_to_message_id=reply_to_message_id,
            )
            voice_file.close()  # release BytesIO internal buffer
            del voice_file

            logging.info(
                "Voice reply sent: chat_id=%s, %d bytes OGG Opus",
                chat_id,
                ogg_size,
            )
        except Exception as e:
            logging.error("Failed to send voice reply to Telegram: %s", e)
    finally:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass


def fire_voice_reply(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    response_text: str,
    *,
    voice: str = "Aoede",
) -> None:
    """Fire-and-forget: schedule voice reply as a retryable background task.

    Called by ai_chat.py after text streaming completes.
    Uses submit_retryable for automatic retry on transient failures.
    """
    _bot = bot
    _chat_id = chat_id
    _reply_to = reply_to_message_id
    _text = response_text
    _voice = voice

    def _factory() -> Coroutine[Any, Any, None]:
        return _generate_and_send_voice(
            _bot,
            _chat_id,
            _reply_to,
            _text,
            voice=_voice,
        )

    submit_retryable(_factory, retry=2)

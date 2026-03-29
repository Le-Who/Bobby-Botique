# /app/voice_engine.py
"""Voice Engine 3.0 — orchestrates TTS generation and Telegram voice delivery.

Provides a single entry point for the chat handler to fire-and-forget
voice reply generation after text streaming completes.

Strategy: text → sentence-boundary chunking → parallel REST TTS calls →
           PCM concatenation → ffmpeg → OGG Opus → bot.send_voice()

Chunking ensures full-text voicing for any response length без hard truncation.
"""

import asyncio
import logging

from telegram import Bot

from app.utils.background_tasks import submit_retryable


async def _generate_single_chunk(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
) -> bytes | None:
    """Generate PCM audio for a single text chunk with key rotation.

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
            pcm = await generate_speech(text_chunk, key_data["api_key"], voice=voice)
            if pcm:
                return pcm
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
      1. Clean & chunk text at sentence boundaries (≤1500 chars/chunk)
      2. Generate PCM for each chunk (parallel with shared key rotation)
      3. Concatenate raw PCM buffers (same sample rate → simple append)
      4. Transcode PCM → OGG Opus
      5. Send voice message

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
        # 1. Clean & chunk
        clean_text = _clean_text_for_speech(response_text)
        if not clean_text:
            logging.warning("Voice reply: cleaned text is empty (chat_id=%s)", chat_id)
            return

        chunks = _chunk_text_by_sentences(clean_text, max_chars=1500)
        logging.info(
            "Voice reply: %d chars → %d chunk(s) for TTS",
            len(clean_text),
            len(chunks),
        )

        # 2. Generate PCM for each chunk (share the failed_keys set across chunks)
        failed_keys: set[str] = set()

        if len(chunks) == 1:
            # Fast path: single chunk, no concurrency overhead
            pcm_audio = await _generate_single_chunk(chunks[0], voice, failed_keys)
            if pcm_audio is None:
                logging.warning("No audio generated for voice reply (chat_id=%s)", chat_id)
                return
        else:
            # Parallel generation for multi-chunk texts
            # Use a semaphore to limit concurrency to 3 parallel TTS calls
            sem = asyncio.Semaphore(3)

            async def _bounded_generate(chunk: str) -> bytes | None:
                async with sem:
                    return await _generate_single_chunk(chunk, voice, failed_keys)

            results = await asyncio.gather(*[_bounded_generate(c) for c in chunks])

            # Concatenate PCM buffers in order (all share 24kHz 16-bit mono format)
            pcm_parts = [r for r in results if r is not None]
            if not pcm_parts:
                logging.warning("No audio generated for any chunk (chat_id=%s)", chat_id)
                return

            if len(pcm_parts) < len(chunks):
                logging.warning(
                    "Voice reply: %d/%d chunks failed, voicing partial text",
                    len(chunks) - len(pcm_parts),
                    len(chunks),
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

    def _factory():
        return _generate_and_send_voice(
            _bot,
            _chat_id,
            _reply_to,
            _text,
            voice=_voice,
        )

    submit_retryable(_factory, retry=2)

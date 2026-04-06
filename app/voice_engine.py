# /app/voice_engine.py
"""Voice Engine 4.1 — orchestrates TTS generation and Telegram voice delivery.

Provides a single entry point for the chat handler to fire-and-forget
voice reply generation after text streaming completes.

Strategy (Atomic Router):
  1. ElevenLabs TTS pipeline (primary — best quality, natural voices)
     text → byte-based sentence chunking (≤4500 UTF-8 bytes/chunk)
     → sequential ElevenLabs REST calls (PCM 24kHz) → PCM concatenation

  2. Gemini TTS fallback (if ElevenLabs keys are absent or all quota-exhausted)
     text → byte-based sentence chunking (≤3500 UTF-8 bytes/chunk)
     → sequential Gemini REST TTS calls → PCM concatenation

  3. PCM buffer → ffmpeg → OGG Opus → bot.send_voice()

Atomicity guarantee:
  If ElevenLabs fails mid-stream (any chunk), the entire PCM accumulation is
  discarded and the FULL message is re-synthesized via Gemini.  This prevents
  the user from ever hearing two different voices in a single message.

Sequential TTS calls are mandatory for both providers:
  - Free Tier ElevenLabs keys: tight per-minute character limits
  - Free Tier Gemini keys: ~10 RPD cap; burst parallel calls exhaust it
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from telegram import Bot

from app.utils.background_tasks import submit_retryable

# ─── Gemini TTS pipeline (unchanged, retained as fallback) ───────────────────


async def _generate_single_chunk_gemini(
    text_chunk: str,
    voice: str,
    failed_keys: set[str],
    timeout: float = 120.0,
) -> bytes | None:
    """Generate PCM audio for a single text chunk via Gemini TTS with key rotation.

    Args:
        text_chunk:  Pre-cleaned text fragment to synthesise.
        voice:       Prebuilt Gemini TTS voice name.
        failed_keys: Shared set of key hashes to skip (mutated in place).
        timeout:     HTTP timeout forwarded to generate_speech.

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
                "Gemini TTS chunk failed (attempt %d/3, %d chars): %s",
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


async def _run_gemini_pipeline(
    chunks: list[str],
    voice: str,
    adaptive_timeout: float,
) -> list[bytes] | None:
    """Run the full Gemini TTS pipeline over all chunks.

    Returns a list of PCM byte blobs (possibly partial on mid-stream failure),
    or None if the very first chunk fails (no audio without opening context).
    """
    failed_keys: set[str] = set()
    pcm_parts: list[bytes] = []

    from app.utils.audio import trim_trailing_silence

    for i, chunk in enumerate(chunks):
        pcm = await _generate_single_chunk_gemini(chunk, voice, failed_keys, timeout=adaptive_timeout)
        if pcm:
            pcm_parts.append(trim_trailing_silence(pcm))
        else:
            if i == 0:
                logging.warning("Gemini TTS: first chunk failed, aborting")
            else:
                logging.warning(
                    "Gemini TTS: chunk %d/%d failed, sending partial audio",
                    i + 1,
                    len(chunks),
                )
            break

    return pcm_parts if pcm_parts else None


# ─── Main orchestrator ────────────────────────────────────────────────────────


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
      1. Clean text and split at sentence boundaries
      2. Attempt ElevenLabs pipeline (if keys are configured)
         → On any quota failure: discard PCM, fall back to Gemini
      3. Gemini TTS pipeline as fallback
      4. Concatenate raw PCM buffers (same 24kHz 16-bit mono format)
      5. Transcode PCM → OGG Opus
      6. Send voice message

    Failure policy:
      - ElevenLabs quota/all-keys-fail → silent fallback to Gemini (no UX disruption)
      - Both pipelines fail → edit status message to inform the user
      - First chunk only fails → abort (no audio without opening context)

    Memory-critical: aggressively deletes intermediate buffers at each pipeline
    stage to avoid OOM on the 500MB RAM budget.
    """
    from app.config import settings
    from app.providers.elevenlabs_tts import (
        ELEVENLABS_CHUNK_MAX_BYTES,
        generate_speech_with_key_rotation,
    )
    from app.providers.tts import _chunk_text_by_sentences, _clean_text_for_speech
    from app.utils.audio import crossfade_pcm_chunks, make_voice_file, pcm_to_ogg_opus

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
        # 1. Clean text (shared cleaner removes Markdown, URLs, code blocks, etc.)
        clean_text = _clean_text_for_speech(response_text)
        if not clean_text:
            logging.warning("Voice reply: cleaned text is empty (chat_id=%s)", chat_id)
            return

        # 2. Resolve ElevenLabs configuration
        el_keys = settings.ELEVENLABS_API_KEYS
        el_voice_id = voice if voice and len(voice) > 10 else settings.ELEVENLABS_VOICE_ID
        use_elevenlabs = bool(el_keys)

        pcm_parts: list[bytes] | None = None
        provider_used = "none"

        # ── Branch A: ElevenLabs primary ──────────────────────────────────
        if use_elevenlabs:
            # ElevenLabs accepts up to 5000 bytes; we use 4500 for headroom.
            el_chunks = _chunk_text_by_sentences(clean_text, max_bytes=ELEVENLABS_CHUNK_MAX_BYTES)
            logging.info(
                "Voice reply (ElevenLabs): %d chars / %d bytes → %d chunk(s)",
                len(clean_text),
                len(clean_text.encode("utf-8")),
                len(el_chunks),
            )

            # Adaptive timeout: ~1 s per 50 chars + 15 s base; capped at [30, 90].
            el_timeout = min(90.0, max(30.0, len(clean_text) / 50.0 + 15.0))

            el_pcm_parts = await generate_speech_with_key_rotation(
                el_chunks,
                el_keys,
                voice_id=el_voice_id,
                timeout=el_timeout,
            )

            if el_pcm_parts:
                pcm_parts = el_pcm_parts
                provider_used = "elevenlabs"
            else:
                # ElevenLabs failed (quota exhausted or all keys tried).
                # Atomic fallback: discard everything and use Gemini for the full message.
                logging.info(
                    "Voice reply: ElevenLabs unavailable — falling back to Gemini TTS (chat_id=%s)",
                    chat_id,
                )

        # ── Branch B: Gemini TTS (primary if no EL keys, fallback otherwise) ─
        if pcm_parts is None:
            # Gemini models accept 8192 tokens (~30k chars), but generating long audio
            # streams frequently results in 500 Internal errors and timeouts (40+ seconds).
            # Sequential chunking safely batches audio generation to maintain stability.
            gemini_chunks = _chunk_text_by_sentences(clean_text, max_bytes=2500)
            logging.info(
                "Voice reply (Gemini TTS): %d chars / %d bytes → %d chunk(s)",
                len(clean_text),
                len(clean_text.encode("utf-8")),
                len(gemini_chunks),
            )

            # Adaptive timeout: ~1 s per 60 chars + 15 s base; capped at [30, 120].
            gemini_timeout = min(120.0, max(30.0, len(clean_text) / 60.0 + 15.0))

            gemini_voice = voice if voice and len(voice) <= 10 else "Aoede"
            gemini_pcm_parts = await _run_gemini_pipeline(gemini_chunks, gemini_voice, gemini_timeout)

            if gemini_pcm_parts:
                pcm_parts = gemini_pcm_parts
                provider_used = "gemini"

        # ── No audio generated ─────────────────────────────────────────────
        if not pcm_parts:
            logging.warning("No audio generated for voice reply (chat_id=%s)", chat_id)
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

        logging.info(
            "Voice reply: %d PCM chunk(s) from %s (chat_id=%s)",
            len(pcm_parts),
            provider_used,
            chat_id,
        )

        # 4. Concatenate raw PCM buffers with cross-fade at chunk boundaries
        #    to smooth tonal discontinuities between independent TTS calls
        pcm_audio = crossfade_pcm_chunks(pcm_parts)
        del pcm_parts  # free list references

        logging.info("Voice reply: TTS audio generated (%d bytes PCM)", len(pcm_audio))

        # 5. Transcode PCM → OGG Opus
        ogg_bytes = await pcm_to_ogg_opus(pcm_audio)
        del pcm_audio  # ⚠ Free PCM immediately — OGG is 10-20x smaller

        if ogg_bytes is None:
            logging.error("PCM→OGG transcoding failed for voice reply")
            return

        ogg_size = len(ogg_bytes)

        # 6. Send voice message
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
                "Voice reply sent: chat_id=%s, %d bytes OGG Opus, provider=%s",
                chat_id,
                ogg_size,
                provider_used,
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

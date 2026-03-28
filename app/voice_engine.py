# /app/voice_engine.py
"""Voice Engine 2.0 — orchestrates TTS generation and Telegram voice delivery.

Provides a single entry point for the chat handler to fire-and-forget
voice reply generation after text streaming completes.

Strategy: Live API first (affective audio) → REST TTS fallback (reliable).
           PCM audio → ffmpeg → OGG Opus → bot.send_voice()
"""

import logging

from telegram import Bot

from app.utils.background_tasks import submit_retryable


async def _generate_and_send_voice(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    response_text: str,
    api_key: str,
    *,
    use_live_api: bool = True,
    system_instruction: str | None = None,
    voice: str = "Kore",
) -> None:
    """Generate TTS audio and send as Telegram voice message.

    Tries Live API first (if enabled), falls back to REST TTS.
    Shows 'record_voice' chat action while generating.

    Memory-critical: aggressively deletes intermediate buffers at each pipeline
    stage to avoid OOM on the 500MB RAM budget.
    """
    from app.utils.audio import make_voice_file, pcm_to_ogg_opus

    pcm_audio: bytes | None = None

    # Show recording action to user (non-blocking, best effort)
    try:
        await bot.send_chat_action(chat_id, action="record_voice")
    except Exception:
        pass

    # Truncate long texts for TTS (keeps first ~2000 chars)
    tts_text = response_text[:2000] if len(response_text) > 2000 else response_text

    # ── Strategy 1: Live API (affective, emotional audio) ────────────────
    if use_live_api:
        try:
            from app.providers.live_audio import generate_audio_dialog

            pcm_audio, _transcript = await generate_audio_dialog(
                tts_text,
                api_key,
                system_instruction=system_instruction,
                voice=voice,
            )
            del _transcript  # free transcript immediately, we don't use it here
            if pcm_audio:
                logging.info("Voice reply: using Live API audio (%d bytes PCM)", len(pcm_audio))
        except Exception as e:
            logging.warning("Live Audio failed, falling back to REST TTS: %s", e)

    # ── Strategy 2: REST TTS (reliable fallback) ─────────────────────────
    if pcm_audio is None:
        try:
            from app.providers.tts import generate_speech

            pcm_audio = await generate_speech(tts_text, api_key, voice=voice)
            if pcm_audio:
                logging.info("Voice reply: using REST TTS audio (%d bytes PCM)", len(pcm_audio))
        except Exception as e:
            logging.error("REST TTS generation failed: %s", e)
            return

    if pcm_audio is None:
        logging.warning("No audio generated for voice reply (chat_id=%s)", chat_id)
        return

    # ── Transcode PCM → OGG Opus ─────────────────────────────────────────
    ogg_bytes = await pcm_to_ogg_opus(pcm_audio)
    # ⚠ Free PCM immediately — OGG is 10-20x smaller
    del pcm_audio

    if ogg_bytes is None:
        logging.error("PCM→OGG transcoding failed for voice reply")
        return

    ogg_size = len(ogg_bytes)

    # ── Send voice message ───────────────────────────────────────────────
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


def fire_voice_reply(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    response_text: str,
    api_key: str,
    *,
    use_live_api: bool = True,
    system_instruction: str | None = None,
    voice: str = "Kore",
) -> None:
    """Fire-and-forget: schedule voice reply as a retryable background task.

    Called by ai_chat.py after text streaming completes.
    Uses submit_retryable for automatic retry on transient failures.
    """
    # Closure captures for the factory
    _bot = bot
    _chat_id = chat_id
    _reply_to = reply_to_message_id
    _text = response_text
    _key = api_key
    _live = use_live_api
    _si = system_instruction
    _voice = voice

    def _factory():
        return _generate_and_send_voice(
            _bot,
            _chat_id,
            _reply_to,
            _text,
            _key,
            use_live_api=_live,
            system_instruction=_si,
            voice=_voice,
        )

    submit_retryable(_factory, retry=2)

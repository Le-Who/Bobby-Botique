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
        # Truncate long texts for TTS (keeps first ~2000 chars)
        tts_text = response_text[:2000] if len(response_text) > 2000 else response_text

        # ── Strategy 1: Live API (affective, emotional audio) ────────────────
        failed_keys: set[str] = set()
        from app.errors import classify_key_error
        from app.handlers.ai_core import _resolve_ai_request
        from app.repos.keys import get_key_status_manager

        status_mgr = get_key_status_manager()

        if use_live_api and False:  # FIXME: Temporarily disabled due to API Studio key timeouts
            from app.providers.live_audio import generate_audio_dialog

            for attempt in range(3):
                key_data, model_used, _ = await _resolve_ai_request(
                    "gemini-2.5-flash-native-audio-preview-12-2025", excluded_key_hashes=failed_keys
                )
                if not key_data:
                    break

                try:
                    # Enforce strict TTS mode to prevent conversational hallucinations
                    strict_tts_sys_prompt = (
                        "You are a strict Text-to-Speech (TTS) reading engine. Your ONLY objective is to read "
                        "the user's text EXACTLY word-for-word aloud. You MUST NOT act like a conversational bot. "
                        "Do NOT introduce yourself, do NOT answer questions in the text, and do NOT add or omit any words. "
                        "Just read the script provided."
                    )
                    if system_instruction:
                        strict_tts_sys_prompt += f"\n\nContext tone: {system_instruction}"

                    pcm_audio, _transcript = await generate_audio_dialog(
                        tts_text,
                        key_data["api_key"],
                        system_instruction=strict_tts_sys_prompt,
                        voice=voice,
                    )
                    del _transcript  # free transcript immediately, we don't use it here
                    if pcm_audio:
                        logging.info("Voice reply: using Live API audio (%d bytes PCM)", len(pcm_audio))
                        break
                except Exception as e:
                    err_str = str(e)
                    logging.warning("Live Audio failed (attempt %d/3): %s", attempt + 1, err_str)
                    failed_keys.add(key_data["key_hash"])
                    try:
                        err_cat = classify_key_error(err_str)
                        await status_mgr.suspend_key(key_data["key_hash"], model_used, err_cat, err_str[:200])
                    except Exception:
                        pass

        # ── Strategy 2: REST TTS (reliable fallback) ─────────────────────────
        if pcm_audio is None:
            failed_keys.clear()
            from app.providers.tts import generate_speech

            for attempt in range(3):
                key_data, model_used, _ = await _resolve_ai_request(
                    "gemini-2.5-flash-preview-tts", excluded_key_hashes=failed_keys
                )
                if not key_data:
                    break

                try:
                    pcm_audio = await generate_speech(tts_text, key_data["api_key"], voice=voice)
                    if pcm_audio:
                        logging.info("Voice reply: using REST TTS audio (%d bytes PCM)", len(pcm_audio))
                        break
                except Exception as e:
                    err_str = str(e)
                    logging.warning("REST TTS generation failed (attempt %d/3): %s", attempt + 1, err_str)
                    failed_keys.add(key_data["key_hash"])
                    try:
                        err_cat = classify_key_error(err_str)
                        await status_mgr.suspend_key(key_data["key_hash"], model_used, err_cat, err_str[:200])
                    except Exception:
                        pass

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
    _live = use_live_api
    _si = system_instruction
    _voice = voice

    def _factory():
        return _generate_and_send_voice(
            _bot,
            _chat_id,
            _reply_to,
            _text,
            use_live_api=_live,
            system_instruction=_si,
            voice=_voice,
        )

    submit_retryable(_factory, retry=2)

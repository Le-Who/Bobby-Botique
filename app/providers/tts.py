# /app/providers/tts.py
"""Gemini TTS provider — text-to-speech via REST generate_content.

Uses gemini-2.5-flash-preview-tts model (REST API) to generate speech audio.
Returns raw PCM 24kHz 16-bit mono bytes.

This is the reliable primary path for voice replies. The Live API
(live_audio.py) is the advanced path with affective dialog support.
"""

import asyncio
import logging

from google.genai import types

from app.providers.gemini import get_cached_genai_client

TTS_MODEL = "gemini-2.5-flash-preview-tts"

# Available voices: Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr
DEFAULT_VOICE = "Kore"


async def generate_speech(
    text: str,
    api_key: str,
    *,
    voice: str = DEFAULT_VOICE,
    timeout: float = 30.0,
) -> bytes | None:
    """Generate speech audio from text using Gemini TTS REST API.

    The TTS model is text-in, audio-only-out via standard generate_content.
    The prompt must instruct the model to "say" the text — this is how the
    TTS model differentiates from conversational models.

    Args:
        text: Text to synthesize (will be truncated to ~2000 chars).
        api_key: Gemini API key.
        voice: Prebuilt voice name (e.g., "Kore", "Puck").
        timeout: Maximum time to wait for TTS response.

    Returns:
        Raw PCM 24kHz 16-bit mono bytes, or None on failure.
    """
    if not text or not text.strip():
        return None

    client = get_cached_genai_client(api_key)

    # Truncate to avoid timeout on very long texts
    tts_text = text[:2000] if len(text) > 2000 else text

    # TTS models require explicit instruction to speak
    prompt = f"Say the following naturally and expressively:\n\n{tts_text}"

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice,
                )
            )
        ),
    )

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=TTS_MODEL,
                contents=prompt,
                config=config,
            ),
            timeout=timeout,
        )

        # Extract PCM audio from inline_data in response parts
        if (
            response.candidates
            and response.candidates[0].content
            and response.candidates[0].content.parts
        ):
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    audio_bytes = part.inline_data.data
                    logging.info(
                        "TTS generated: voice=%s, text_len=%d, audio_bytes=%d",
                        voice,
                        len(tts_text),
                        len(audio_bytes),
                    )
                    return audio_bytes

        logging.warning("TTS response contained no audio data")
        return None

    except TimeoutError:
        logging.error("TTS generation timed out after %.0fs", timeout)
        return None
    except Exception as e:
        logging.error("TTS generation failed: %s", e)
        return None

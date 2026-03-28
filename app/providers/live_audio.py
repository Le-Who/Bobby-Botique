# /app/providers/live_audio.py
"""Gemini Live API provider — bidirectional audio dialog via WebSocket.

Uses one-shot ephemeral WebSocket sessions to generate affective speech.
The session is opened, a single text turn is sent, audio+transcript
are collected, and the session is closed. No persistent connections.

Models:
  - Primary: gemini-2.5-flash-native-audio-preview-12-2025
  - Fallback: gemini-3.1-flash-live-preview
"""

import asyncio
import logging

from google.genai import types

from app.providers.gemini import get_cached_genai_client

LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
FALLBACK_LIVE_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Kore"


async def generate_audio_dialog(
    text: str,
    api_key: str,
    *,
    system_instruction: str | None = None,
    voice: str = DEFAULT_VOICE,
    timeout: float = 45.0,
) -> tuple[bytes | None, str | None]:
    """Generate audio response via Gemini Live API (ephemeral session).

    Opens a short-lived WebSocket connection, sends text as a single turn,
    collects audio chunks + output transcription, and closes.

    The Live API generates speech with natural intonation and emotional
    awareness (enable_affective_dialog), which produces higher quality
    audio than the REST TTS endpoint.

    Args:
        text: Text input to speak (bot's response text).
        api_key: Gemini API key.
        system_instruction: Optional system prompt for tone/persona.
        voice: Prebuilt voice name.
        timeout: Overall timeout for the entire session.

    Returns:
        (pcm_audio_bytes, transcript_text) — either can be None on failure.
    """
    if not text or not text.strip():
        return None, None

    client = get_cached_genai_client(api_key)

    # Truncate to avoid long sessions
    dialog_text = text[:2000] if len(text) > 2000 else text

    # Configure Live session
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],  # type: ignore[list-item]  # SDK accepts str|Modality
        output_audio_transcription=types.AudioTranscriptionConfig(),
        enable_affective_dialog=True,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=voice,
                )
            )
        ),
    )

    # Inject system instruction if provided
    if system_instruction:
        config.system_instruction = types.Content(
            parts=[types.Part(text=system_instruction)]
        )

    audio_buffer = bytearray()
    transcript_parts: list[str] = []

    # Try primary model, then fallback
    for model_name in [LIVE_MODEL, FALLBACK_LIVE_MODEL]:
        audio_buffer.clear()
        transcript_parts.clear()

        try:
            async with asyncio.timeout(timeout):
                async with client.aio.live.connect(
                    model=model_name, config=config
                ) as session:
                    # Send text input as a single turn
                    await session.send(input=dialog_text, end_of_turn=True)

                    # Collect response chunks
                    async for response in session.receive():
                        # Raw audio data (response.data shorthand)
                        if response.data is not None:
                            audio_buffer.extend(response.data)

                        # Server content (audio parts + transcription)
                        if response.server_content is not None:
                            model_turn = response.server_content.model_turn
                            if model_turn and model_turn.parts:
                                for part in model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        audio_buffer.extend(part.inline_data.data)
                                    if part.text:
                                        transcript_parts.append(part.text)

                            # Turn complete — stop collecting
                            if response.server_content.turn_complete:
                                break

            # Success — convert and free buffer immediately
            pcm_bytes = bytes(audio_buffer) if audio_buffer else None
            buffer_len = len(audio_buffer)
            audio_buffer.clear()  # ⚠ free bytearray memory before return

            transcript = " ".join(transcript_parts).strip() or None

            logging.info(
                "Live Audio Dialog complete: model=%s, audio=%d bytes, transcript=%d chars",
                model_name,
                buffer_len,
                len(transcript) if transcript else 0,
            )
            return pcm_bytes, transcript

        except Exception as e:
            # ⚠ Free buffer on error before trying fallback model
            audio_buffer.clear()
            transcript_parts.clear()

            logging.warning(
                "Live API session failed with %s: %s",
                model_name,
                e,
            )
            # If primary model failed, try fallback
            if model_name == FALLBACK_LIVE_MODEL:
                # Both models failed
                logging.error("All Live API models failed, returning None")
                return None, None
            continue

    return None, None

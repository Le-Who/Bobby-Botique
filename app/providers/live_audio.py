# /app/providers/live_audio.py
"""Gemini Live API provider — one-shot text-to-speech via WebSocket.

Opens an ephemeral WebSocket session, sends text via
``send_client_content`` with ``turn_complete=True``, collects audio
+ transcript, closes.

Model: gemini-3.1-flash-live-preview (primary, with REST TTS fallback
managed by voice_engine.py).
"""

import asyncio
import logging

from google.genai import types

from app.providers.gemini import get_cached_genai_client

LIVE_MODEL = "gemini-3.1-flash-live-preview"
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

    Opens a short-lived WebSocket connection, sends text via
    ``send_client_content`` (the official method for discrete text turns),
    and collects audio chunks + output transcription, then closes.

    ``send_client_content`` with ``turn_complete=True`` bypasses the
    VAD pipeline entirely — unlike ``send_realtime_input``, which routes
    through Voice Activity Detection and hangs indefinitely on text-only
    input waiting for audio silence that never comes.

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
        response_modalities=["AUDIO"],  # type: ignore[list-item]
        output_audio_transcription=types.AudioTranscriptionConfig(),
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
            parts=[types.Part.from_text(text=system_instruction)]
        )

    audio_buffer = bytearray()
    transcript_parts: list[str] = []

    try:
        async with asyncio.timeout(timeout):
            async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                # Send text via send_client_content — the official method for
                # discrete text turns per the Live API docs.  Unlike
                # send_realtime_input(text=...), this does NOT route through
                # the VAD pipeline and immediately signals turn completion.
                await session.send_client_content(
                    turns={"role": "user", "parts": [{"text": dialog_text}]},
                    turn_complete=True,
                )

                # 3. Collect response chunks
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

                        # Output transcription arrives as a separate field,
                        # not inside model_turn.parts (AUDIO modality sessions
                        # don't produce text parts).
                        if response.server_content.output_transcription:
                            t_text = response.server_content.output_transcription.text
                            if t_text:
                                transcript_parts.append(t_text)

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
            LIVE_MODEL,
            buffer_len,
            len(transcript) if transcript else 0,
        )
        return pcm_bytes, transcript

    except Exception as e:
        # ⚠ Free buffer on error
        audio_buffer.clear()
        transcript_parts.clear()

        logging.warning(
            "Live API session failed with %s: %r",
            LIVE_MODEL,
            e,
        )
        return None, None

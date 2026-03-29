# /app/providers/live_audio.py
"""Gemini Live API provider — one-shot text-to-speech via WebSocket.

Opens an ephemeral WebSocket session, sends text via
``send_realtime_input`` with **manual VAD** (activityStart/activityEnd),
collects audio + transcript, closes.

Why manual VAD?
  With automatic VAD (the default), the server detects text as "activity
  start" but never detects "end of turn" for a single text message — it
  keeps waiting for more input, causing the session to hang until timeout.
  Disabling auto-VAD and sending explicit activityStart/activityEnd around
  the text creates clear turn boundaries so the model responds immediately.

Why send_realtime_input, not send_client_content?
  ``send_client_content`` is ONLY supported for seeding initial context
  history (requires ``historyConfig.initialHistoryInClientContent``).
  Using it for new user messages triggers ``APIError(1007 Invalid argument)``.

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
    ``send_realtime_input`` with manual activity signaling, and
    collects audio chunks + output transcription, then closes.

    Protocol:
        1. Disable automatic VAD in session config.
        2. Send ``activityStart`` to mark beginning of user turn.
        3. Send ``text`` via ``send_realtime_input``.
        4. Send ``activityEnd`` to mark end of user turn.
        5. Collect audio + transcript until ``turn_complete``.

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

    # Configure Live session with MANUAL VAD control.
    # Automatic VAD detects text as activity but never detects "end of
    # turn" for a single text message, causing indefinite hanging.
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
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True,
            ),
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
                # Manual VAD protocol:
                # 1. Signal "user started their turn"
                await session.send_realtime_input(
                    activity_start=types.ActivityStart(),
                )

                # 2. Send text input
                await session.send_realtime_input(text=dialog_text)

                # 3. Signal "user finished their turn" — triggers model
                #    response generation immediately.
                await session.send_realtime_input(
                    activity_end=types.ActivityEnd(),
                )

                # 4. Collect response chunks
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


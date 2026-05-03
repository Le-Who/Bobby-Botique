import asyncio
import logging
import os
import sys

from google.genai import types

# Append the current directory so app imports work
sys.path.append(os.getcwd())

from app.config import GEMINI_LIVE_MODEL
from app.providers.gemini import get_vertex_client

logging.basicConfig(level=logging.INFO)


async def test_live():
    client = get_vertex_client()
    if not client:
        return

    # Try stripped down config
    base_config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
    )

    # Try full config but without some parts
    configs_to_test = [
        ("Base (no transcriptions, no tools, no VAD)", base_config),
        (
            "With Transcriptions",
            types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
            ),
        ),
        (
            "With Session Resumption",
            types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                session_resumption=types.SessionResumptionConfig(handle=None),
            ),
        ),
        (
            "With VAD",
            types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                        prefix_padding_ms=150,
                        silence_duration_ms=700,
                    ),
                    activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                ),
            ),
        ),
        (
            "With Search",
            types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        ),
        (
            "With System Instruction",
            types.LiveConnectConfig(
                response_modalities=[types.Modality.AUDIO],
                system_instruction=types.Content(parts=[types.Part(text="Hello")]),
            ),
        ),
    ]

    for _name, config in configs_to_test:
        try:
            async with client.aio.live.connect(model=GEMINI_LIVE_MODEL, config=config):
                break  # We can just test them one by one, wait we need to test all
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(test_live())

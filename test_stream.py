import asyncio
import time

from google import genai
from google.genai import types

from app.config import settings


async def main():
    api_key = settings.GEMINI_API_KEYS[0] if settings.GEMINI_API_KEYS else None
    if not api_key:
        print("No GEMINI_API_KEYS in settings.")
        return
    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Aoede",
                )
            )
        ),
    )

    text = "Hello! This is a test of streaming audio using the Gemini model. " * 8

    print("Sending request...")
    start_time = time.time()

    # Try streaming
    response_stream = await client.aio.models.generate_content_stream(
        model="gemini-2.5-flash-tts",
        contents=text,
        config=config,
    )

    first_chunk = True
    full_audio = b""
    async for chunk in response_stream:
        if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
            for part in chunk.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    audio_data = part.inline_data.data
                    full_audio += audio_data
                    if first_chunk:
                        print(f"TTFB: {time.time() - start_time:.2f}s, first chunk size: {len(audio_data)}")
                        first_chunk = False
                    else:
                        print(f"Received chunk size: {len(audio_data)} at {time.time() - start_time:.2f}s")

    print(f"Total time: {time.time() - start_time:.2f}s, total audio size: {len(full_audio)}")


asyncio.run(main())

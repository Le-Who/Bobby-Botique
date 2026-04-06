import asyncio
import os

os.environ["ADMIN_ID"] = "123"  # mock admin to load settings
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

    text = "Привет! Это проверка генерации звука. " * 200

    print(f"Sending request length: {len(text)} chars, {len(text.encode('utf-8'))} bytes")
    start_time = time.time()

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash-tts",
            contents=text,
            config=config,
        )
        audio = response.candidates[0].content.parts[0].inline_data.data
        print(f"Success! Audio size: {len(audio)} bytes. Time: {time.time() - start_time:.2f}s")
    except Exception as e:
        print(f"Error: {e}")


asyncio.run(main())

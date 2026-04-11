import asyncio
import io
import os


class MockAIOClient:
    class MockModels:
        async def generate_content(self, model, contents, config):
            print("CALLED MOCK GENERATE_CONTENT")
            print(f"Contents: {contents}")
            print(f"Temp in Config: {config.temperature}")

            # Return dummy response structure to avoid exceptions
            class MockCandidate:
                class MockContent:
                    class MockPart:
                        class MockInlineData:
                            data = b"fake_pcm_data"

                        inline_data = MockInlineData()

                    parts = [MockPart()]

                content = MockContent()

            class MockResponse:
                candidates = [MockCandidate()]

            return MockResponse()

    def __init__(self):
        self.models = self.MockModels()


class MockClient:
    def __init__(self):
        self.aio = MockAIOClient()


import app.providers.tts as tts

tts.get_cached_genai_client = lambda key: MockClient()


async def main():
    import app.voice_engine as ve

    chunks = ["First sentence.", "Second sentence."]
    pcm_parts = await ve._run_gemini_pipeline(chunks, "Aoede", 30.0, 0.8)
    print(f"Generated chunks: {len(pcm_parts)}")


asyncio.run(main())

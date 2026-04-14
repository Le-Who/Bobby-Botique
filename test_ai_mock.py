import asyncio
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, r"d:\gemaibotv2\gemaibotv2")

# Mock settings
mock_settings = MagicMock()
mock_settings.POLLINATIONS_DEFAULT_IMAGE_MODEL = "flux"
mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
mock_settings.GEMINI_API_KEYS = ["MOCK_KEY"]

with patch.dict("sys.modules"):
    # Inject mocked settings

    # We gotta mock the actual config module dict
    sys.modules["app.config"].settings = mock_settings

    from app.handlers.cmd_image import check_draw_intent_async

    # Mock the AI extraction since we don't have a real key here
    async def mock_extract(text):
        if "такую же" in text:
            return "леса"
        return None

    import app.handlers.cmd_image

    app.handlers.cmd_image._extract_draw_prompt_ai = mock_extract

    async def main():
        test_cases = [
            "Бот, сгенерируй мне картинки по такой теме: Космос",
            "Привет, бот, создай изображение, где будет Кот",
            "Нарисуй кота",
            "Я сегодня видел картинку леса. Нарисуй такую же пожалуйста",
            "Слушай, сделай фото где мы с тобой",
            "Привет, как дела?",
            "Нарисуй что-нибудь",
        ]

        for tc in test_cases:
            res = await check_draw_intent_async(tc)
            print(f"[{'MATCH' if res else 'FAIL'}] '{tc}' -> {res}")

    if __name__ == "__main__":
        asyncio.run(main())

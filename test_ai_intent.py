import asyncio
import os
import sys

# Setup environment variables needed for tests
os.environ["POLLINATIONS_DEFAULT_IMAGE_MODEL"] = "flux"
os.environ["TELEGRAM_MESSAGE_LIMIT"] = "4096"
sys.path.insert(0, r"d:\gemaibotv2\gemaibotv2")

from app.handlers.cmd_image import check_draw_intent_async


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

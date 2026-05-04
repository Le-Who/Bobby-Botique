import asyncio
import logging
from dotenv import load_dotenv

load_dotenv(".env")

from app import database as db
from app.providers.pollinations import get_pollinations_provider
from app.bot_instance import register_bot
from telegram import Bot
import os

async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("NO TOKEN")
        return
    bot = Bot(token)
    register_bot(bot)
    
    provider = get_pollinations_provider()
    print("Generating image...")
    res = await provider.generate("A cute cat", width=1024, height=1024, model="zimage")
    if not res.success:
        print("GENERATE FAILED:", res.error_message)
        return
    print("Generate success. Bytes:", len(res.images[0]))
    
    from app.config import settings
    try:
        print("Sending photo...")
        msg = await bot.send_photo(chat_id=settings.CONFIG_CHAT_ID, photo=res.images[0])
        print("Success!", msg.photo[-1].file_id)
    except Exception as e:
        print("ERROR SENDING PHOTO:", repr(e))

asyncio.run(main())

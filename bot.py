import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application
from flask import Flask
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Импортируем наши модули
from app import config, database
from app.handlers import commands, messages, callbacks

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "I am alive!", 200

async def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    if not all([config.TELEGRAM_BOT_TOKEN, config.GEMINI_API_KEYS, config.DATABASE_URL, config.TAVILY_API_KEYS]):
        logging.warning("One or more environment variables are not set! Bot may have limited functionality.")

    logging.info("Initializing database...")
    await database.init_db()
    logging.info("Database initialized.")

    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрация обработчиков
    commands.register(application)
    callbacks.register(application)
    messages.register(application)

    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"0.0.0.0:{config.PORT}"]
    
    logging.info(f"Health check server will run on port {config.PORT}.")
    logging.info("Starting Telegram bot polling...")

    try:
        await asyncio.gather(
            serve(flask_app, hypercorn_config),
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        )
    except Exception as e:
        logging.critical(f"Application failed: {e}", exc_info=True)
    finally:
        if database.db_pool:
            await database.db_pool.close()
        logging.info("Database pool closed.")

if __name__ == "__main__":
    asyncio.run(main())

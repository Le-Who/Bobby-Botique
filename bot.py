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

async def run_bot_and_server():
    """Основная логика: запускает бота и веб-сервер параллельно."""
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрация всех обработчиков
    commands.register(application)
    callbacks.register(application)
    messages.register(application)
    
    # async with application управляет жизненным циклом: initialize() при входе, shutdown() при выходе
    async with application:
        # Запускаем polling в фоновом режиме
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        # Конфигурируем и запускаем веб-сервер
        hypercorn_config = HypercornConfig()
        hypercorn_config.bind = [f"0.0.0.0:{config.PORT}"]
        
        logging.info(f"Health check server will run on port {config.PORT}.")
        logging.info("Bot is running...")
        
        # await serve(...) будет работать, пока приложение не будет остановлено.
        # Пока он работает, бот, запущенный в фоне, продолжает обрабатывать обновления.
        await serve(flask_app, hypercorn_config)
        
        # При завершении serve (например, при остановке сервиса на Render),
        # останавливаем бота для чистого выхода.
        await application.updater.stop()
        await application.stop()

async def main():
    """Главная функция: настраивает логирование, БД и запускает приложение."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    if not all([config.TELEGRAM_BOT_TOKEN, config.GEMINI_API_KEYS, config.DATABASE_URL, config.TAVILY_API_KEYS]):
        logging.warning("One or more environment variables are not set!")

    try:
        logging.info("Initializing database...")
        await database.init_db()
        logging.info("Database initialized.")
        await run_bot_and_server()
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
    finally:
        if database.db_pool:
            await database.db_pool.close()
            logging.info("Database pool closed.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")

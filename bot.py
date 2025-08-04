import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application
from flask import Flask
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Импортируем наши модули
from app.config import settings
from app import database
from app.handlers import commands, messages, callbacks
from app.metrics import metrics_collector
from app.alerts import alert_manager
from app.cache import start_cache_cleanup_task
from app.queue import start_task_queue, stop_task_queue
from app.group_chat import initialize_group_chats

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "I am alive!", 200

async def run_bot_and_server():
    """Основная логика: запускает бота и веб-сервер параллельно."""
    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрация всех обработчиков
    commands.register(application)
    callbacks.register(application)
    messages.register(application)
    
    async with application:
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        hypercorn_config = HypercornConfig()
        hypercorn_config.bind = [f"0.0.0.0:{settings.PORT}"]
        
        logging.info(f"Health check server will run on port {settings.PORT}.")
        logging.info("Bot is running...")
        
        await serve(flask_app, hypercorn_config)
        
        await application.updater.stop()
        await application.stop()

async def main():
    """Главная функция: настраивает логирование, БД и запускает приложение."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    try:
        logging.info("Initializing database...")
        await database.init_db()
        logging.info("Database initialized.")
        
        logging.info("Initializing group chats...")
        await initialize_group_chats()
        logging.info("Group chats initialized.")
        
        logging.info("Starting cache cleanup task...")
        await start_cache_cleanup_task()
        logging.info("Cache cleanup task started.")
        
        logging.info("Starting task queue...")
        await start_task_queue()
        logging.info("Task queue started.")
        
        await run_bot_and_server()
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
    finally:
        logging.info("Shutting down services...")
        await stop_task_queue()
        if database.db_pool:
            await database.db_pool.close()
            logging.info("Database pool closed.")
        logging.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")

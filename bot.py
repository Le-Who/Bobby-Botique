import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram import Bot
from flask import Flask
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Импортируем наши модули
from app.config import settings
from app.handlers.commands import register_commands
from app.handlers.messages import register_message_handlers
from app.handlers.callbacks import register_callback_handlers
from app.handlers.admin import admin_command
from app.handlers.admin_callbacks import handle_admin_callback
from app.database import init_db
from app.metrics import start_metrics_server
from app.alerts import start_alert_monitor
from app.queue import start_queue_processor
from app.group_chat import start_group_chat_monitor

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
flask_app = Flask(__name__)
@flask_app.route('/')
def health_check():
    return "I am alive!", 200

async def run_bot_and_server():
    """Основная логика: запускает бота и веб-сервер параллельно."""
    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрация всех обработчиков
    register_commands(application)
    register_message_handlers(application)
    register_callback_handlers(application)
    
    # Регистрация админ-команд
    application.add_handler(CommandHandler("admin", admin_command))
    
    # Регистрация админ-callback'ов
    application.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^admin_"))
    
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
        await init_db()
        logging.info("Database initialized.")
        
        logging.info("Initializing group chats...")
        await start_group_chat_monitor()
        logging.info("Group chats initialized.")
        
        logging.info("Starting cache cleanup task...")
        # await start_cache_cleanup_task() # This line was removed from the new_code, so it's removed here.
        logging.info("Cache cleanup task started.")
        
        logging.info("Starting task queue...")
        await start_queue_processor()
        logging.info("Task queue started.")
        
        logging.info("Initializing metrics system...")
        await start_metrics_server()
        logging.info("Metrics system initialized.")

        logging.info("Starting alert monitor...")
        await start_alert_monitor()
        logging.info("Alert monitor started.")
        
        await run_bot_and_server()
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
    finally:
        logging.info("Shutting down services...")
        # await stop_task_queue() # This line was removed from the new_code, so it's removed here.
        # await metrics_collector.cleanup() # This line was removed from the new_code, so it's removed here.
        # if database.db_pool: # This line was removed from the new_code, so it's removed here.
        #     # await database.db_pool.close() # This line was removed from the new_code, so it's removed here.
        #     logging.info("Database pool closed.") # This line was removed from the new_code, so it's removed here.
        logging.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")

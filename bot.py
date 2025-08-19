import os
import logging
import asyncio
import signal
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from flask import Flask
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Импортируем наши модули
from app.config import settings
from app import database
from app.handlers import commands, messages, callbacks
from app.handlers.callbacks import new_topic_callback
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

# Глобальная переменная для управления завершением
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    logging.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

async def health_check_database():
    """Периодическая проверка состояния базы данных"""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(300)  # Каждые 5 минут
            if shutdown_event.is_set():
                break
            await database.db_query("SELECT 1")  # Простой запрос для проверки
            logging.info("Database health check passed")
        except Exception as e:
            logging.error(f"Database health check failed: {e}")
            # Попытка переподключения
            await database.reconnect_database()

async def run_bot_and_server():
    """Основная логика: запускает бота и веб-сервер параллельно."""
    application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    
    # Регистрация всех обработчиков
    commands.register(application)
    callbacks.register(application)
    messages.register(application)
    application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic$"))
    
    async with application:
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        hypercorn_config = HypercornConfig()
        hypercorn_config.bind = [f"0.0.0.0:{settings.PORT}"]
        
        logging.info(f"Health check server will run on port {settings.PORT}.")
        logging.info("Bot is running...")
        
        # Запускаем мониторинг БД в фоне
        db_monitor_task = asyncio.create_task(health_check_database())
        
        try:
            await serve(flask_app, hypercorn_config)
        except Exception as e:
            logging.error(f"Web server error: {e}")
        finally:
            # Отменяем мониторинг БД
            db_monitor_task.cancel()
            try:
                await db_monitor_task
            except asyncio.CancelledError:
                pass
            
            await application.updater.stop()
            await application.stop()

async def main():
    """Главная функция: настраивает логирование, БД и запускает приложение."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        logging.info("Starting bot initialization...")
        
        logging.info("Initializing database...")
        await database.init_db()
        logging.info("Database initialized successfully.")
        
        logging.info("Initializing group chats...")
        await initialize_group_chats()
        logging.info("Group chats initialized.")
        
        logging.info("Starting cache cleanup task...")
        await start_cache_cleanup_task()
        logging.info("Cache cleanup task started.")
        
        logging.info("Starting task queue...")
        await start_task_queue()
        logging.info("Task queue started.")
        
        logging.info("Initializing metrics system...")
        await metrics_collector.initialize()
        logging.info("Metrics system initialized.")
        
        logging.info("Starting main application loop...")
        await run_bot_and_server()
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
        # Добавить отправку уведомления администратору
        await _notify_admin_of_crash(e)
    finally:
        logging.info("Shutting down services...")
        await stop_task_queue()
        await metrics_collector.cleanup()
        if database.db_pool:
            await database.db_pool.close()
            logging.info("Database pool closed.")
        logging.info("Shutdown complete.")

async def _notify_admin_of_crash(error: Exception):
    """Уведомляет администратора о критической ошибке"""
    try:
        # Здесь можно добавить отправку уведомления администратору
        # Например, через Telegram API или email
        logging.critical(f"CRITICAL ERROR - Bot crashed: {error}")
        # В будущем можно добавить отправку сообщения администратору
    except Exception as e:
        logging.error(f"Failed to notify admin of crash: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")

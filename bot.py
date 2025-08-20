import os
import logging
import asyncio
import signal
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter
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
    
    # Для Render важно правильно обработать SIGTERM
    if signum == signal.SIGTERM:
        logging.info("SIGTERM received - Render is shutting down the service")
    elif signum == signal.SIGINT:
        logging.info("SIGINT received - User interrupted the service")

async def basic_monitoring():
    """Базовый мониторинг работы бота"""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(300)  # Каждые 5 минут
            if shutdown_event.is_set():
                break
            
            # Простая проверка базы данных
            try:
                await database.db_query("SELECT 1")
                logging.info("Database connection: OK")
            except Exception as e:
                logging.error(f"Database connection issue: {e}")
                # Попытка переподключения
                await database.reconnect_database()
            
            # Логируем статус бота
            logging.info("Bot monitoring: All systems operational")
                    
        except Exception as e:
            logging.error(f"Monitoring error: {e}")

async def run_bot_with_retry():
    """Запускает бота с автоматическими повторами при сетевых ошибках"""
    max_retries = 5
    base_delay = 1  # секунды
    application = None
    
    for attempt in range(max_retries):
        try:
            # Настройка таймаутов через Application.builder()
            # Создаем Application с кастомными настройками Request
            from telegram.request import HTTPXRequest
            
            # Создаем кастомный Request объект
            custom_request = HTTPXRequest(
                connection_pool_size=8,
                connect_timeout=10.0,  # 10 секунд на подключение
                read_timeout=30.0,     # 30 секунд на чтение
                write_timeout=30.0,    # 30 секунд на запись
                pool_timeout=30.0      # 30 секунд на получение соединения из пула
            )
            
            # Создаем Application с кастомным Request
            application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).request(custom_request).build()
            
            # Регистрация всех обработчиков
            commands.register(application)
            callbacks.register(application)
            messages.register(application)
            application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic$"))
            
            # Запускаем бота без async with для лучшего контроля
            await application.start()
            
            # Настройка polling с улучшенными параметрами
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,  # Игнорируем старые обновления
                timeout=30,  # Таймаут для long polling
                read_timeout=30,  # Таймаут для чтения
                write_timeout=30,  # Таймаут для записи
                connect_timeout=10,  # Таймаут для подключения
                pool_timeout=30,  # Таймаут для получения соединения из пула
            )
            
            logging.info("Bot started successfully")
            
            # Ждем завершения
            while not shutdown_event.is_set():
                await asyncio.sleep(1)
            
            # Graceful shutdown
            logging.info("Shutting down bot gracefully...")
            await application.updater.stop()
            await application.stop()
            break  # Успешное завершение
                
        except (NetworkError, TimedOut, RetryAfter) as e:
            delay = base_delay * (2 ** attempt)  # Экспоненциальная задержка
            logging.warning(f"Network error on attempt {attempt + 1}/{max_retries}: {e}")
            logging.info(f"Retrying in {delay} seconds...")
            
            # Очищаем ресурсы перед повторной попыткой
            if application:
                try:
                    await application.updater.stop()
                    await application.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error: {cleanup_error}")
            
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
            else:
                logging.error(f"Max retries ({max_retries}) reached. Bot failed to start.")
                raise
        except Exception as e:
            logging.error(f"Unexpected error during bot startup: {e}")
            
            # Очищаем ресурсы при ошибке
            if application:
                try:
                    await application.updater.stop()
                    await application.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error: {cleanup_error}")
            
            raise

async def run_bot_and_server():
    """Основная логика: запускает бота и веб-сервер параллельно."""
    
    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"0.0.0.0:{settings.PORT}"]
    
    logging.info(f"Health check server will run on port {settings.PORT}.")
    logging.info("Bot is running...")
    
    # Запускаем базовый мониторинг в фоне
    monitoring_task = asyncio.create_task(basic_monitoring())
    
    # Запускаем бота с обработкой ошибок
    bot_task = asyncio.create_task(run_bot_with_retry())
    
    # Создаем задачу для веб-сервера
    server_task = asyncio.create_task(serve(flask_app, hypercorn_config))
    
    try:
        # Ждем завершения любой из задач
        done, pending = await asyncio.wait(
            [monitoring_task, bot_task, server_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        logging.info("One of the main tasks completed, initiating shutdown...")
        
    except Exception as e:
        logging.error(f"Error in main loop: {e}")
    finally:
        # Graceful shutdown всех задач
        logging.info("Starting graceful shutdown...")
        
        # Отменяем все задачи
        for task in [monitoring_task, bot_task, server_task]:
            if not task.done():
                task.cancel()
        
        # Ждем завершения всех задач
        await asyncio.gather(
            monitoring_task, bot_task, server_task,
            return_exceptions=True
        )
        
        logging.info("All tasks shutdown complete")

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
    except asyncio.CancelledError:
        logging.info("Main application loop was cancelled - this is normal during shutdown")
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
        # Добавить отправку уведомления администратору
        await _notify_admin_of_crash(e)
    finally:
        logging.info("Shutting down services...")
        try:
            await stop_task_queue()
        except Exception as e:
            logging.warning(f"Error stopping task queue: {e}")
        
        try:
            await metrics_collector.cleanup()
        except Exception as e:
            logging.warning(f"Error cleaning up metrics: {e}")
        
        if database.db_pool:
            try:
                await database.db_pool.close()
                logging.info("Database pool closed.")
            except Exception as e:
                logging.warning(f"Error closing database pool: {e}")
        
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
    except asyncio.CancelledError:
        logging.info("Bot was cancelled - this is normal during shutdown")
    except Exception as e:
        logging.critical(f"Unexpected error in main: {e}", exc_info=True)
        # Для Render важно логировать все критические ошибки
        import sys
        sys.exit(1)

import os
import logging
import asyncio
import signal
import datetime
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
    """Health check endpoint для Render Free Tier"""
    print("Health check request received from Render", flush=True)
    logging.info("Health check request received from Render")
    return "I am alive!", 200

@flask_app.route('/status')
def status_check():
    """Расширенная проверка статуса для диагностики"""
    try:
        print("Status check request received", flush=True)
        # Проверяем базовые компоненты
        # Проверяем состояние БД
        db_status = "disconnected"
        if database.db_pool:
            if hasattr(database.db_pool, 'is_closed') and database.db_pool.is_closed():
                db_status = "closed"
            else:
                db_status = "connected"
        
        status = {
            "bot": "running",
            "database": db_status,
            "database_error": "blocked_network" if "blocked network" in str(getattr(database, '_last_error', '')) else None,
            "timestamp": str(datetime.datetime.now()),
            "uptime": "active"
        }
        print(f"Status: {status}", flush=True)
        return status, 200
    except Exception as e:
        error_msg = f"Status check error: {e}"
        print(error_msg, flush=True)
        logging.error(error_msg)
        return {"error": str(e)}, 500

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
    
    # Для Render Free Tier важно логировать все попытки
    print(f"Starting bot with retry mechanism (max attempts: {max_retries})", flush=True)
    logging.info(f"Starting bot with retry mechanism (max attempts: {max_retries})")
    
    version_info = Application.__version__ if hasattr(Application, '__version__') else 'Unknown'
    print(f"Python-telegram-bot version: {version_info}", flush=True)
    logging.info(f"Python-telegram-bot version: {version_info}")
    
    for attempt in range(max_retries):
        print(f"Bot startup attempt {attempt + 1}/{max_retries}", flush=True)
        logging.info(f"Bot startup attempt {attempt + 1}/{max_retries}")
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
            # В новой версии python-telegram-bot ОБЯЗАТЕЛЬНО нужно вызывать initialize()
            try:
                await application.initialize()
                logging.info("Application initialized successfully")
            except Exception as init_error:
                logging.error(f"Failed to initialize application: {init_error}")
                raise
            
            try:
                await application.start()
                logging.info("Application started successfully")
            except Exception as start_error:
                logging.error(f"Failed to start application: {start_error}")
                raise
            
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
            logging.info("Bot is now polling for updates...")
            
            # Ждем завершения
            while not shutdown_event.is_set():
                await asyncio.sleep(1)
            
            # Graceful shutdown
            logging.info("Shutting down bot gracefully...")
            logging.info("Stopping updater...")
            await application.updater.stop()
            logging.info("Stopping application...")
            await application.stop()
            logging.info("Bot shutdown complete")
            break  # Успешное завершение
                
        except (NetworkError, TimedOut, RetryAfter) as e:
            delay = base_delay * (2 ** attempt)  # Экспоненциальная задержка
            logging.warning(f"Network error on attempt {attempt + 1}/{max_retries}: {e}")
            logging.info(f"Retrying in {delay} seconds...")
            
            # Очищаем ресурсы перед повторной попыткой
            if application:
                try:
                    # Проверяем, был ли updater запущен
                    if hasattr(application, 'updater') and application.updater:
                        await application.updater.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error (updater): {cleanup_error}")
                
                try:
                    # Проверяем, была ли application инициализирована
                    if hasattr(application, '_initialized') and application._initialized:
                        await application.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error (application): {cleanup_error}")
            
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
                    # Проверяем, был ли updater запущен
                    if hasattr(application, 'updater') and application.updater:
                        await application.updater.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error (updater): {cleanup_error}")
                
                try:
                    # Проверяем, была ли application инициализирована
                    if hasattr(application, '_initialized') and application._initialized:
                        await application.stop()
                except Exception as cleanup_error:
                    logging.warning(f"Cleanup error (application): {cleanup_error}")
            
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
        
        # Сначала останавливаем мониторинг (может использовать БД)
        shutdown_event.set()
        
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
    # Настройка логирования для Render (принудительно в stdout)
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),  # Принудительно в stdout
            logging.FileHandler('/tmp/bot.log')  # Backup в файл
        ]
    )
    
    # Принудительно выводим в stdout для Render
    print("=== BOT STARTUP INITIATED ===", flush=True)
    logging.info("=== BOT STARTUP INITIATED ===")
    
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        print("Starting bot initialization...", flush=True)
        logging.info("Starting bot initialization...")
        
        print("Initializing database...", flush=True)
        logging.info("Initializing database...")
        try:
            await database.init_db()
            print("Database initialized successfully.", flush=True)
            logging.info("Database initialized successfully.")
        except Exception as db_error:
            if "blocked network" in str(db_error).lower() or "neon.tech" in str(db_error).lower():
                print("CRITICAL: Database connection blocked by Neon.tech", flush=True)
                logging.critical("CRITICAL: Database connection blocked by Neon.tech")
                print("Bot will start in limited mode without database", flush=True)
                logging.warning("Bot will start in limited mode without database")
                # Продолжаем без БД в ограниченном режиме
            else:
                raise db_error
        
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
        
        # Сначала останавливаем все задачи, которые могут использовать БД
        try:
            await stop_task_queue()
        except Exception as e:
            logging.warning(f"Error stopping task queue: {e}")
        
        # Останавливаем мониторинг
        try:
            shutdown_event.set()
        except Exception as e:
            logging.warning(f"Error setting shutdown event: {e}")
        
        # Останавливаем метрики (может использовать БД)
        try:
            await metrics_collector.cleanup()
        except Exception as e:
            logging.warning(f"Error cleaning up metrics: {e}")
        
        # В последнюю очередь закрываем пул БД
        if database.db_pool and not database.db_pool.is_closed():
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
    print("=== BOT MAIN ENTRY POINT ===", flush=True)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped by user.", flush=True)
        logging.info("Bot stopped by user.")
    except asyncio.CancelledError:
        print("Bot was cancelled - this is normal during shutdown", flush=True)
        logging.info("Bot was cancelled - this is normal during shutdown")
    except Exception as e:
        error_msg = f"Unexpected error in main: {e}"
        print(error_msg, flush=True)
        logging.critical(error_msg, exc_info=True)
        # Для Render важно логировать все критические ошибки
        import sys
        sys.exit(1)

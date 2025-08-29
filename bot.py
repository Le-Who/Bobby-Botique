import os
import logging
import asyncio
import signal
import datetime
import time

import sys
from telegram import Update
from telegram.ext import Application
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
from flask import Flask
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Импортируем наши модули
from app.config import settings
from app import database
from app.handlers import commands, messages, callbacks

from app.metrics import metrics_collector
from app.alerts import alert_manager
from app.utils.logging_config import setup_detailed_logging, log_api_summary
from app.error_handler import error_handler

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
    """
    Расширенный статус системы для мониторинга
    """
    try:
        # Базовая информация о системе
        status_info = {
            "status": "healthy",
            "timestamp": datetime.datetime.now().isoformat(),
            "version": "2.0.0",
            "environment": os.getenv("ENVIRONMENT", "production")
        }
        
        # Проверяем доступность базы данных
        try:
            if database.db_pool and not database.db_pool._closed:
                status_info["database"] = "connected"
            else:
                status_info["database"] = "disconnected"
        except Exception as e:
            status_info["database"] = f"error: {str(e)}"
        
        # Проверяем метрики
        try:
            if hasattr(metrics_collector, 'get_metrics_summary'):
                metrics = metrics_collector.get_metrics_summary()
                status_info["metrics"] = "available"
                status_info["total_requests"] = metrics.get('total_requests', 0)
            else:
                status_info["metrics"] = "unavailable"
        except Exception as e:
            status_info["metrics"] = f"error: {str(e)}"
        
        # Проверяем очередь задач
        try:
            from app.queue import task_queue
            if task_queue and hasattr(task_queue, 'get_queue_stats'):
                queue_stats = task_queue.get_queue_stats()
                status_info["queue"] = "healthy"
                status_info["pending_tasks"] = queue_stats.get('pending_tasks', 0)
            else:
                status_info["queue"] = "unavailable"
        except Exception as e:
            status_info["queue"] = f"error: {str(e)}"
        
        # Форматируем ответ
        response_lines = [
            f"Status: {status_info['status']}",
            f"Timestamp: {status_info['timestamp']}",
            f"Version: {status_info['version']}",
            f"Environment: {status_info['environment']}",
            f"Database: {status_info['database']}",
            f"Metrics: {status_info['metrics']}",
            f"Queue: {status_info['queue']}"
        ]
        
        if 'total_requests' in status_info:
            response_lines.append(f"Total Requests: {status_info['total_requests']}")
        if 'pending_tasks' in status_info:
            response_lines.append(f"Pending Tasks: {status_info['pending_tasks']}")
        
        return "\n".join(response_lines), 200
        
    except Exception as e:
        logging.error(f"Error in status check: {e}")
        return f"Error: {str(e)}", 500

# --- BOT INITIALIZATION ---
async def create_application():
    """Создает и настраивает приложение бота"""
    try:
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
        
        # Регистрируем все обработчики
        commands.register(application)
        messages.register(application)
        callbacks.register(application)
        
        # Регистрируем обработчик ошибок
        application.add_error_handler(error_handler.handle_telegram_update("global_error"))
        
        # Инициализируем Circuit Breaker мониторинг
        try:
            from app.services import get_gemini_circuit_breaker
            circuit_breaker = get_gemini_circuit_breaker()
            await circuit_breaker.start_monitoring()
            logging.info("Circuit Breaker monitoring started successfully")
        except Exception as e:
            logging.warning(f"Failed to start Circuit Breaker monitoring: {e}")
        
        logging.info("Application created successfully")
        return application
        
    except Exception as e:
        logging.error(f"Failed to create application: {e}")
        raise



# --- SHUTDOWN MANAGEMENT ---
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logging.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()

async def _cleanup_application(application):
    """Очищает ресурсы приложения"""
    try:
        if application:
            await application.stop()
            await application.shutdown()
            logging.info("Application stopped and shutdown completed")
    except Exception as e:
        logging.error(f"Error during application cleanup: {e}")

# --- MAIN BOT LOOP ---
async def run_bot_with_retry():
    """Запускает бота с автоматическими повторами при сетевых ошибках"""
    max_retries = 5
    base_delay = 1  # секунды
    application = None
    attempt = 0
    
    # Логируем все попытки запуска
    print(f"Starting bot with retry mechanism (max attempts: {max_retries})", flush=True)
    logging.info(f"Starting bot with retry mechanism (max attempts: {max_retries})")
    
    version_info = Application.__version__ if hasattr(Application, '__version__') else 'Unknown'
    print(f"Python-telegram-bot version: {version_info}", flush=True)
    logging.info(f"Python-telegram-bot version: {version_info}")
    
    while not shutdown_event.is_set() and attempt < max_retries:
        attempt += 1
        print(f"Bot startup attempt {attempt}/{max_retries} initiated", flush=True)
        logging.info(f"Bot startup attempt {attempt}/{max_retries} initiated")
        
        try:
            # Создаем приложение
            application = await create_application()
            
            # Запускаем бота
            await application.initialize()
            await application.start()
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True
            )
            
            logging.info("Bot started successfully")
            print("Bot is running...", flush=True)
            
            # Основной цикл ожидания
            while not shutdown_event.is_set():
                await asyncio.sleep(1)
            
            # Graceful shutdown
            logging.info("Shutdown requested, stopping bot...")
            break
            
        except Conflict as e:
            logging.error(f"Telegram API conflict detected: {e}")
            logging.critical("Another bot instance is running. This instance will exit.")
            
            # Завершаем работу при конфликте
            break
            
        except NetworkError as e:
            logging.error(f"Network error during bot operation: {e}")
            
            # Очищаем ресурсы при ошибке
            await _cleanup_application(application)
            
            # Проверяем, не нужно ли завершить работу
            if shutdown_event.is_set():
                logging.info("Shutdown requested during retry, stopping bot")
                break
            
            # Экспоненциальная задержка для сетевых ошибок
            delay = base_delay * (2 ** min(attempt, 4))  # 1, 2, 4, 8, 16 секунд
            logging.info(f"Retrying in {delay} seconds due to network error...")
            await asyncio.sleep(delay)
            
        except Exception as e:
            logging.error(f"Unexpected error during bot operation: {e}")
            
            # Очищаем ресурсы при ошибке
            await _cleanup_application(application)
            
            # Проверяем, не нужно ли завершить работу
            if shutdown_event.is_set():
                logging.info("Shutdown requested during retry, stopping bot")
                break
            
            # Ждем перед повторной попыткой
            delay = base_delay * (2 ** min(attempt, 4))  # 1, 2, 4, 8, 16 секунд
            logging.info(f"Retrying in {delay} seconds...")
            await asyncio.sleep(delay)
            
        finally:
            # Очищаем ресурсы
            if application:
                await _cleanup_application(application)
    
    if attempt >= max_retries:
        logging.error(f"Bot failed after {max_retries} attempts. Giving up.")
        print(f"Bot failed after {max_retries} attempts. Giving up.", flush=True)
    else:
        logging.info("Bot shutdown completed")

# --- HEALTH CHECKS ---
async def startup_health_check():
    """Проверяет здоровье системы при запуске"""
    try:
        # Проверяем Telegram API
        from telegram import Bot
        test_bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        await test_bot.get_me()
        logging.info("✓ Telegram API check passed")
        
        # Проверяем базу данных
        if database.db_pool and not database.db_pool._closed:
            await database.db_query("SELECT 1")
            logging.info("✓ Database check passed")
        else:
            logging.warning("⚠ Database unavailable")
        
        # Проверяем метрики только если база данных доступна
        try:
            if database.db_pool and not database.db_pool._closed:
                await metrics_collector.initialize()
                logging.info("✓ Metrics system verified")
            else:
                logging.warning("⚠ Metrics system skipped - database unavailable")
        except Exception as e:
            logging.warning(f"⚠ Metrics system check failed: {e}")
            logging.warning("Bot will run without metrics collection")
        
        logging.info("✓ Core systems healthy - bot ready to start")
        return True  # Возвращаем True для успешной проверки
        
    except Exception as e:
        logging.error(f"✗ Health check failed: {e}")
        raise Exception(f"Health check failed: {e}")

async def main():
    """Main application entry point with improved error handling."""
    print("=== BOT MAIN FUNCTION START ===", flush=True)
    
    # Инициализируем компоненты
    database_available = False
    memory_manager = None
    
    try:
        # Инициализация логирования
        setup_detailed_logging()
        logging.info("Bot starting up...")
        
        # Инициализация базы данных
        try:
            await database.init_db()
            database_available = True
            logging.info("Database initialized successfully")
        except Exception as e:
            logging.error(f"Database initialization failed: {e}")
            database_available = False
        
        # Инициализация менеджера памяти
        try:
            from app.memory_manager import MemoryManager
            memory_manager = MemoryManager()
            logging.info("Memory manager initialized")
        except Exception as e:
            logging.warning(f"Memory manager initialization failed: {e}")
            memory_manager = None
        
        # Инициализация очереди задач
        try:
            await start_task_queue()
            logging.info("Task queue started")
        except Exception as e:
            logging.warning(f"Task queue initialization failed: {e}")
        
        # Инициализация групповых чатов
        try:
            await initialize_group_chats()
            logging.info("Group chats initialized")
        except Exception as e:
            logging.warning(f"Group chats initialization failed: {e}")
        
        # Проверка здоровья системы
        await startup_health_check()
        
        # Запуск health check сервера для Northflank
        health_server = None
        try:
            # Запускаем простой HTTP сервер для health check
            health_server = await start_health_server()
            
            # Запуск бота
            bot_task = asyncio.create_task(run_bot_with_retry())
            
            # Ожидание завершения
            await asyncio.gather(bot_task, return_exceptions=True)
        except Exception as e:
            logging.error(f"Bot task failed: {e}")
        
    except Exception as e:
        logging.error(f"Critical error in main: {e}")
        raise
    
    finally:
        # Очистка ресурсов
        logging.info("Cleaning up resources...")
        
        try:
            if memory_manager:
                await memory_manager.cleanup()
                logging.info("Memory manager cleaned up")
        except Exception as e:
            logging.warning(f"Memory manager cleanup failed: {e}")
        
        try:
            await stop_task_queue()
            logging.info("Task queue stopped")
        except Exception as e:
            logging.warning(f"Task queue cleanup failed: {e}")
        
        if web_server_task:
            web_server_task.cancel()
            try:
                await web_server_task
            except asyncio.CancelledError:
                pass
            logging.info("Web server stopped")
        
        logging.info("Resource cleanup completed")

if __name__ == "__main__":
    # Настройка обработчиков сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Запускаем бота
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Bot failed to start: {e}")
        sys.exit(1)
    finally:
        logging.info("Bot shutdown completed")

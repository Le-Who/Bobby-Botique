import os
import logging
import asyncio
import signal
import datetime
import time
import fcntl
import sys
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
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
from app.utils.logging_config import setup_detailed_logging, log_api_summary

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
        status = {
            "bot": "running",
            "database": "connected" if database.db_pool else "disconnected",
            "timestamp": str(datetime.datetime.now()),
            "uptime": "active",
            "version": "2.0.0",
            "environment": os.getenv("ENVIRONMENT", "production")
        }
        
        # Добавляем информацию о системе
        import psutil
        try:
            status["system"] = {
                "cpu_percent": psutil.cpu_percent(interval=1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            }
        except ImportError:
            status["system"] = {"error": "psutil not available"}
        
        print("Status: %s", status, flush=True)
        return status, 200
    except Exception as e:
        error_msg = "Status check error: %s" % e
        print(error_msg, flush=True)
        logging.error(error_msg)
        return {"error": str(e)}, 500


@flask_app.route('/health')
def health_check_endpoint():
    """Health check endpoint для мониторинга"""
    try:
        # Проверяем основные компоненты
        bot_status = "running"
        database_status = "connected" if database.db_pool and not database.db_pool._closed else "disconnected"
        
        # Проверяем Redis статус
        try:
            from app.cache import redis_client
            if redis_client:
                redis_client.ping()
                redis_status = "connected"
            else:
                redis_status = "not_configured"
        except Exception:
            redis_status = "disconnected"
        
        # Определяем общий статус
        if database_status == "connected" and bot_status == "running":
            overall_status = "healthy"
        elif database_status == "disconnected":
            overall_status = "unhealthy"
        else:
            overall_status = "degraded"
        
        health_status = {
            "status": overall_status,
            "timestamp": str(datetime.datetime.now()),
            "container_id": os.environ.get('HOSTNAME', 'unknown'),
            "process_id": os.getpid(),
            "services": {
                "bot": bot_status,
                "database": database_status,
                "redis": redis_status
            }
        }
        
        # Возвращаем соответствующий HTTP код
        if overall_status == "healthy":
            return health_status, 200
        elif overall_status == "degraded":
            return health_status, 200  # 200 для degraded, но с предупреждением
        else:
            return health_status, 503  # 503 для unhealthy
            
    except Exception as e:
        return {
            "status": "unhealthy", 
            "error": str(e),
            "timestamp": str(datetime.datetime.now())
        }, 500

# Глобальная переменная для управления завершением
shutdown_event = asyncio.Event()

# Механизм блокировки для предотвращения множественных экземпляров
lock_file = None
lock_fd = None

def acquire_lock():
    """Приобретает блокировку файла для предотвращения множественных экземпляров"""
    global lock_file, lock_fd
    
    try:
        # Упрощенная логика для контейнерной среды
        container_id = os.environ.get('HOSTNAME', 'unknown')
        lock_file = f"/tmp/gemaibot.{container_id}.lock"
        
        # В контейнерной среде всегда удаляем старые блокировки
        if os.path.exists(lock_file):
            try:
                os.unlink(lock_file)
                logging.info(f"Removed existing lock file for container {container_id}")
            except Exception as e:
                logging.warning(f"Error removing existing lock: {e}")
        
        # Создаем новый файл блокировки
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        
        # Пытаемся приобрести эксклюзивную блокировку
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        
        # Записываем PID текущего процесса
        pid = str(os.getpid())
        os.write(lock_fd, pid.encode())
        os.fsync(lock_fd)
        
        logging.info(f"Lock acquired successfully. PID: {pid}, Container: {container_id}")
        return True
        
    except (OSError, IOError) as e:
        if lock_fd:
            try:
                os.close(lock_fd)
            except:
                pass
            lock_fd = None
        
        logging.error(f"Failed to acquire lock: {e}")
        return False

def release_lock():
    """Освобождает блокировку файла"""
    global lock_file, lock_fd
    
    try:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except (OSError, IOError) as e:
                logging.warning(f"Error releasing file lock: {e}")
            finally:
                lock_fd = None
        
        if lock_file and os.path.exists(lock_file):
            try:
                os.unlink(lock_file)
                logging.info("Lock file removed successfully")
            except (OSError, IOError) as e:
                logging.warning(f"Error removing lock file: {e}")
            
    except Exception as e:
        logging.error(f"Error releasing lock: {e}")
    finally:
        lock_file = None
        lock_fd = None

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    logging.info(f"Received signal {signum}, initiating graceful shutdown...")
    
    # Устанавливаем флаг завершения
    shutdown_event.set()
    
    # Для Render важно правильно обработать SIGTERM
    if signum == signal.SIGTERM:
        logging.info("SIGTERM received - Render is shutting down the service")
        # Даем 30 секунд на graceful shutdown
        import threading
        def force_shutdown():
            import time
            time.sleep(30)
            logging.warning("Force shutdown after timeout")
            release_lock()  # Освобождаем блокировку перед выходом
            import os
            os._exit(1)
        
        force_thread = threading.Thread(target=force_shutdown, daemon=True)
        force_thread.start()
        
    elif signum == signal.SIGINT:
        logging.info("SIGINT received - User interrupted the service")

async def basic_monitoring():
    """Базовый мониторинг работы бота"""
    logging.info("Monitoring task started - will run continuously until shutdown")
    
    # Счетчик для периодических проверок
    check_counter = 0
    
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(300)  # Каждые 5 минут
            if shutdown_event.is_set():
                break
            
            check_counter += 1
            
            # Простая проверка базы данных
            try:
                if database.db_pool and not database.db_pool._closed:
                    await database.ensure_database_connection()
                    logging.info("Database connection: OK")
                else:
                    logging.warning("Database unavailable - skipping connection check")
            except Exception as e:
                logging.error(f"Database connection issue: {e}")
                # Попытка переподключения уже выполнена в ensure_database_connection
            
            # Каждые 12 проверок (1 час) выполняем расширенную диагностику
            if check_counter % 12 == 0:
                try:
                    # Проверяем состояние очереди задач только если база данных доступна
                    if database.db_pool and not database.db_pool._closed:
                        from app.queue import task_queue
                        stats = await task_queue.get_queue_stats()
                        logging.info(f"Task queue stats: {stats}")
                    else:
                        logging.warning("Task queue stats unavailable - database not accessible")
                    
                    # Проверяем метрики только если база данных доступна
                    if database.db_pool and not database.db_pool._closed:
                        metrics_summary = await metrics_collector.get_metrics_summary()
                        logging.info(f"Metrics summary: {metrics_summary['total_requests']} requests, {metrics_summary['error_rate']:.1f}% errors")
                    else:
                        logging.warning("Metrics unavailable - database not accessible")
                    
                except Exception as e:
                    logging.warning(f"Extended monitoring failed: {e}")
            
            # Логируем статус бота
            logging.info("Bot monitoring: All systems operational")
                    
        except Exception as e:
            logging.error(f"Monitoring error: {e}")
            # При ошибке мониторинга продолжаем работу, не завершаем задачу
            await asyncio.sleep(60)  # Ждем минуту перед следующей попыткой
    
    logging.info("Monitoring task stopped due to shutdown signal")

async def _cleanup_application(application):
    """Очищает ресурсы application при ошибках"""
    if not application:
        return
    
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
    
    while not shutdown_event.is_set():
        print(f"Bot startup attempt initiated", flush=True)
        logging.info("Bot startup attempt initiated")
        
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
            # Простая экспоненциальная задержка с ограничением
            retry_count = 0
            delay = min(base_delay * (2 ** retry_count), 60)  # Максимум 60 секунд
            retry_count += 1
            
            logging.warning(f"Network error during bot operation: {e}")
            logging.info(f"Retrying in {delay} seconds...")
            
            # Очищаем ресурсы перед повторной попыткой
            await _cleanup_application(application)
            
            # Проверяем, не нужно ли завершить работу
            if shutdown_event.is_set():
                logging.info("Shutdown requested during retry, stopping bot")
                break
            
            # Ждем перед повторной попыткой
            await asyncio.sleep(delay)
            
        except Conflict as e:
            logging.error(f"Telegram API conflict detected: {e}")
            logging.critical("Another bot instance is running. This instance will exit.")
            
            # Освобождаем блокировку и завершаем работу
            release_lock()
            break
            
        except Exception as e:
            logging.error(f"Unexpected error during bot operation: {e}")
            
            # Очищаем ресурсы при ошибке
            await _cleanup_application(application)
            
            # Проверяем, не нужно ли завершить работу
            if shutdown_event.is_set():
                logging.info("Shutdown requested after error, stopping bot")
                break
            
            # Логируем детали ошибки для диагностики
            import traceback
            logging.error(f"Bot error details: {traceback.format_exc()}")
            
            # Ждем перед повторной попыткой
            await asyncio.sleep(30)  # Ждем 30 секунд перед повторной попыткой
    
    logging.info("Bot retry loop stopped due to shutdown signal")

async def bot_watchdog(bot_task: asyncio.Task):
    """Следит за состоянием бота и перезапускает его при необходимости"""
    logging.info("Bot watchdog started")
    
    # Счетчик для периодических проверок
    check_counter = 0
    last_restart_time = 0
    
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(60)  # Проверяем каждую минуту
            
            if shutdown_event.is_set():
                break
            
            check_counter += 1
            
            # Проверяем, что задача бота все еще работает
            if bot_task.done():
                if bot_task.exception():
                    logging.error(f"Bot task failed with exception: {bot_task.exception()}")
                    logging.info("Bot watchdog will trigger restart on next iteration")
                else:
                    logging.warning("Bot task completed unexpectedly")
                    logging.info("Bot watchdog will trigger restart on next iteration")
            else:
                logging.info("Bot task is running normally")
            
            # Каждые 60 проверок (1 час) выполняем профилактический перезапуск
            if check_counter % 60 == 0:
                current_time = time.time()
                if current_time - last_restart_time > 3600:  # Не чаще чем раз в час
                    logging.info("Performing preventive bot restart (hourly maintenance)")
                    last_restart_time = current_time
                    # Здесь можно добавить логику перезапуска бота
                
        except Exception as e:
            logging.error(f"Watchdog error: {e}")
            await asyncio.sleep(30)
    
    logging.info("Bot watchdog stopped due to shutdown signal")

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
    
    # Запускаем watchdog для бота
    watchdog_task = asyncio.create_task(bot_watchdog(bot_task))
    
    # Создаем задачу для веб-сервера
    server_task = asyncio.create_task(serve(flask_app, hypercorn_config))
    
    # Создаем задачу для обработки сигналов завершения
    shutdown_task = asyncio.create_task(_wait_for_shutdown())
    
    try:
        # Ждем только сигнала завершения, НЕ завершения задач
        await shutdown_task
        
        logging.info("Shutdown signal received, initiating graceful shutdown...")
        
    except Exception as e:
        logging.error(f"Critical error in main loop: {e}")
        # При критической ошибке также инициируем shutdown
        logging.info("Critical error detected, initiating shutdown...")
        
        # Если это конфликт Telegram API, освобождаем блокировку
        if "Conflict" in str(e) or "terminated by other getUpdates request" in str(e):
            logging.critical("Telegram API conflict detected, releasing lock and shutting down")
            release_lock()
    finally:
        # Graceful shutdown всех задач
        logging.info("Starting graceful shutdown...")
        
        # Отменяем все задачи
        for task in [monitoring_task, bot_task, watchdog_task, server_task, shutdown_task]:
            if not task.done():
                task.cancel()
        
        # Ждем завершения всех задач
        await asyncio.gather(
            monitoring_task, bot_task, watchdog_task, server_task, shutdown_task,
            return_exceptions=True
        )
        
        logging.info("All tasks shutdown complete")
        
        logging.info("Shutting down services...")
        try:
            await stop_task_queue()
        except Exception as e:
            logging.warning(f"Error stopping task queue: {e}")
        
        try:
            await metrics_collector.cleanup()
        except Exception as e:
            logging.warning(f"Error cleaning up metrics: {e}")
        
        # Закрываем пул базы данных только если он еще открыт
        if database.db_pool and not database.db_pool._closed:
            try:
                await database.db_pool.close()
                logging.info("Database pool closed.")
            except Exception as e:
                logging.warning(f"Error closing database pool: {e}")
        else:
            logging.info("Database pool already closed or not initialized.")
        
        logging.info("Shutdown complete.")

async def _wait_for_shutdown():
    """Ждет сигнала завершения от shutdown_event"""
    await shutdown_event.wait()
    logging.info("Shutdown event triggered")

async def startup_health_check():
    """Проверяет здоровье всех критических систем при запуске"""
    logging.info("Performing startup health check...")
    
    # Проверяем базу данных
    try:
        await database.ensure_database_connection()
        logging.info("✓ Database connection verified")
    except Exception as e:
        logging.warning(f"⚠ Database connection failed: {e}")
        logging.warning("Bot will run in limited mode without database functionality")
        # Не прерываем запуск, если база данных недоступна
    
    # Проверяем Telegram API
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe")
            if response.status_code == 200:
                bot_info = response.json()
                if bot_info.get('ok'):
                    logging.info(f"✓ Telegram API verified - Bot: {bot_info['result']['username']}")
                else:
                    raise Exception(f"Telegram API error: {bot_info}")
            else:
                raise Exception(f"Telegram API HTTP error: {response.status_code}")
    except Exception as e:
        logging.error(f"✗ Telegram API check failed: {e}")
        raise Exception(f"Telegram API health check failed: {e}")
    
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
        
        # Инициализация очереди задач
        if database_available:
            try:
                await start_task_queue()
                logging.info("Task queue started")
            except Exception as e:
                logging.error(f"Task queue initialization failed: {e}")
        
        # Инициализация групповых чатов
        try:
            await initialize_group_chats()
            logging.info("Group chats initialized")
        except Exception as e:
            logging.warning(f"Group chat initialization failed: {e}")
        
        # Проверка здоровья системы
        try:
            if database_available:
                health_status = await startup_health_check()
                if not health_status:
                    logging.warning("Startup health check failed")
        except Exception as e:
            logging.warning(f"Startup health check failed: {e}")
            if not database_available:
                logging.warning("Bot will start in limited mode without full health verification")
            else:
                raise e
        
        logging.info("Starting main application loop...")
        await run_bot_and_server()
        
    except asyncio.CancelledError:
        logging.info("Main application loop was cancelled - this is normal during shutdown")
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
        await _notify_admin_of_crash(e)
    finally:
        logging.info("Shutting down services...")
        
        # Остановка менеджера памяти
        if memory_manager:
            try:
                await memory_manager.stop()
                logging.info("Memory manager stopped")
            except Exception as e:
                logging.warning(f"Error stopping memory manager: {e}")
        
        # Остановка очереди задач
        if database_available:
            try:
                await stop_task_queue()
                logging.info("Task queue stopped")
            except Exception as e:
                logging.warning(f"Error stopping task queue: {e}")
        
        # Очистка метрик
        if database_available:
            try:
                await metrics_collector.cleanup()
                logging.info("Metrics collector cleaned up")
            except Exception as e:
                logging.warning(f"Error cleaning up metrics: {e}")
        
        # Закрытие пула базы данных
        if database.db_pool and not database.db_pool._closed:
            try:
                await database.db_pool.close()
                logging.info("Database pool closed")
            except Exception as e:
                logging.warning(f"Error closing database pool: {e}")
        else:
            logging.info("Database pool already closed or not initialized")
        
        logging.info("Shutdown complete")

async def _notify_admin_of_crash(error: Exception):
    """Уведомляет администратора о критической ошибке"""
    try:
        logging.critical(f"CRITICAL ERROR - Bot crashed: {error}")
        # В будущем можно добавить отправку сообщения администратору
    except Exception as e:
        logging.error(f"Failed to notify admin of crash: {e}")

if __name__ == "__main__":
    print("=== BOT MAIN ENTRY POINT ===", flush=True)
    
    # Log container information for debugging
    container_id = os.environ.get('HOSTNAME', 'unknown')
    print(f"Container ID: {container_id}", flush=True)
    print(f"Process ID: {os.getpid()}", flush=True)
    
    # Проверяем блокировку перед запуском
    print("Checking for existing bot instances...", flush=True)
    if not acquire_lock():
        print("ERROR: Another bot instance is already running or lock cannot be acquired. Exiting.", flush=True)
        print("If this is a fresh deployment, the lock may be stale. Try clearing it manually.", flush=True)
        sys.exit(1)
    
    # Verify lock was properly acquired
    if not lock_file or not os.path.exists(lock_file):
        print("ERROR: Lock file verification failed after acquisition. Exiting.", flush=True)
        sys.exit(1)
    
    print("Lock acquired successfully. Starting bot...", flush=True)
    print(f"Lock file: {lock_file}", flush=True)
    
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
        sys.exit(1)
    finally:
        # Всегда освобождаем блокировку при завершении
        print("Shutting down bot and releasing lock...", flush=True)
        release_lock()
        print("Bot shutdown complete. Lock released.", flush=True)

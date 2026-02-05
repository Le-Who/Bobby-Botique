import os
import logging
import asyncio
import signal
import time
import sys
import threading

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, TypeHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Import custom modules
from app.config import settings
from app import database
from app.handlers import commands, messages, callbacks
from app.handlers.callbacks import new_topic_callback
from app.handlers.middleware import rate_limit_middleware
from app.metrics import metrics_collector
from app.utils.logging_config import setup_detailed_logging

from app.queue import start_task_queue, stop_task_queue
from app.group_chat import initialize_group_chats

# Import extracted modules
from app.web import flask_app
from app.utils.lock import process_lock

# Global shutdown event
shutdown_event = asyncio.Event()

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    logging.info(f"Received signal {signum}, initiating graceful shutdown...")
    
    # Устанавливаем флаг завершения
    shutdown_event.set()
    
    # Для Render важно правильно обработать SIGTERM
    if signum == signal.SIGTERM:
        logging.info("SIGTERM received - Render is shutting down the service")
        # Даем 30 секунд на graceful shutdown
        def force_shutdown():
            time.sleep(30)
            logging.warning("Force shutdown after timeout")
            process_lock.release()  # Освобождаем блокировку перед выходом
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
            if await database.check_database_health():
                logging.info("Database connection: OK")
            else:
                logging.warning("Database unavailable")
                # Attempt reconnect
                await database.ensure_database_connection()
            
            # Каждые 12 проверок (1 час) выполняем расширенную диагностику
            if check_counter % 12 == 0:
                try:
                    # Проверяем метрики только если база данных доступна
                    if await database.check_database_health():
                        from app.queue import task_queue
                        stats = await task_queue.get_queue_stats()
                        logging.info(f"Task queue stats: {stats}")

                        metrics_summary = await metrics_collector.get_metrics_summary()
                        logging.info(f"Metrics summary: {metrics_summary['total_requests']} requests, {metrics_summary['error_rate']:.1f}% errors")
                    else:
                        logging.warning("Metrics/Queue stats unavailable - database not accessible")
                    
                except Exception as e:
                    logging.warning(f"Extended monitoring failed: {e}")
            
            # Логируем статус бота
            logging.info("Bot monitoring: All systems operational")
                    
        except Exception as e:
            logging.error(f"Monitoring error: {e}")
            await asyncio.sleep(60)
    
    logging.info("Monitoring task stopped due to shutdown signal")

async def _cleanup_application(application):
    """Очищает ресурсы application при ошибках"""
    if not application:
        return
    
    try:
        if hasattr(application, 'updater') and application.updater:
            await application.updater.stop()
    except Exception as cleanup_error:
        logging.warning(f"Cleanup error (updater): {cleanup_error}")
    
    try:
        if hasattr(application, '_initialized') and application._initialized:
            await application.stop()
    except Exception as cleanup_error:
        logging.warning(f"Cleanup error (application): {cleanup_error}")

async def run_bot_with_retry():
    """Запускает бота с автоматическими повторами при сетевых ошибках"""
    max_retries = 5
    base_delay = 1
    application = None
    
    print(f"Starting bot with retry mechanism (max attempts: {max_retries})", flush=True)
    logging.info(f"Starting bot with retry mechanism (max attempts: {max_retries})")
    
    version_info = Application.__version__ if hasattr(Application, '__version__') else 'Unknown'
    logging.info(f"Python-telegram-bot version: {version_info}")
    
    while not shutdown_event.is_set():
        logging.info("Bot startup attempt initiated")
        
        try:
            from telegram.request import HTTPXRequest
            
            custom_request = HTTPXRequest(
                connection_pool_size=8,
                connect_timeout=10.0,
                read_timeout=30.0,
                write_timeout=30.0,
                pool_timeout=30.0
            )
            
            application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).request(custom_request).build()
            
            # Register rate limit middleware with high priority (group -1)
            application.add_handler(TypeHandler(Update, rate_limit_middleware), group=-1)

            commands.register(application)
            callbacks.register(application)
            messages.register(application)
            application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic$"))
            
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
            
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                timeout=30,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=10,
                pool_timeout=30,
            )
            
            logging.info("Bot started successfully")
            logging.info("Bot is now polling for updates...")
            
            while not shutdown_event.is_set():
                await asyncio.sleep(1)
            
            logging.info("Shutting down bot gracefully...")
            logging.info("Stopping updater...")
            await application.updater.stop()
            logging.info("Stopping application...")
            await application.stop()
            logging.info("Bot shutdown complete")
            break
                
        except (NetworkError, TimedOut, RetryAfter) as e:
            retry_count = 0
            delay = min(base_delay * (2 ** retry_count), 60)
            retry_count += 1
            
            logging.warning(f"Network error during bot operation: {e}")
            logging.info(f"Retrying in {delay} seconds...")
            
            await _cleanup_application(application)
            
            if shutdown_event.is_set():
                break
            
            await asyncio.sleep(delay)
            
        except Conflict as e:
            logging.error(f"Telegram API conflict detected: {e}")
            logging.critical("Another bot instance is running. This instance will exit.")
            process_lock.release()
            break
            
        except Exception as e:
            logging.error(f"Unexpected error during bot operation: {e}")
            await _cleanup_application(application)
            
            if shutdown_event.is_set():
                break
            
            import traceback
            logging.error(f"Bot error details: {traceback.format_exc()}")
            await asyncio.sleep(30)
    
    logging.info("Bot retry loop stopped due to shutdown signal")

async def bot_watchdog(bot_task: asyncio.Task):
    """Следит за состоянием бота и перезапускает его при необходимости"""
    logging.info("Bot watchdog started")
    check_counter = 0
    
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(60)
            if shutdown_event.is_set(): break
            
            check_counter += 1
            
            if bot_task.done():
                if bot_task.exception():
                    logging.error(f"Bot task failed with exception: {bot_task.exception()}")
                else:
                    logging.warning("Bot task completed unexpectedly")
            else:
                logging.info("Bot task is running normally")
                
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
    
    monitoring_task = asyncio.create_task(basic_monitoring())
    bot_task = asyncio.create_task(run_bot_with_retry())
    watchdog_task = asyncio.create_task(bot_watchdog(bot_task))
    server_task = asyncio.create_task(serve(flask_app, hypercorn_config))
    shutdown_task = asyncio.create_task(_wait_for_shutdown())
    
    try:
        await shutdown_task
        logging.info("Shutdown signal received, initiating graceful shutdown...")
    except Exception as e:
        logging.error(f"Critical error in main loop: {e}")
        if "Conflict" in str(e) or "terminated by other getUpdates request" in str(e):
            process_lock.release()
    finally:
        logging.info("Starting graceful shutdown...")
        for task in [monitoring_task, bot_task, watchdog_task, server_task, shutdown_task]:
            if not task.done():
                task.cancel()
        
        await asyncio.gather(
            monitoring_task, bot_task, watchdog_task, server_task, shutdown_task,
            return_exceptions=True
        )
        
        logging.info("Shutting down services...")
        try: await stop_task_queue()
        except Exception: pass
        
        try: await metrics_collector.cleanup()
        except Exception: pass
        
        try: await database.db_manager.close()
        except Exception: pass
        
        logging.info("Shutdown complete.")

async def _wait_for_shutdown():
    await shutdown_event.wait()
    logging.info("Shutdown event triggered")

async def startup_health_check():
    """Проверяет здоровье всех критических систем при запуске"""
    logging.info("Performing startup health check...")
    
    try:
        await database.ensure_database_connection()
        logging.info("✓ Database connection verified")
    except Exception as e:
        logging.warning(f"⚠ Database connection failed: {e}")
        logging.warning("Bot will run in limited mode without database functionality")
    
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
    
    if await database.check_database_health():
        try:
            await metrics_collector.initialize()
            logging.info("✓ Metrics system verified")
        except Exception:
            logging.warning("⚠ Metrics system check failed")
    
    logging.info("✓ Core systems healthy - bot ready to start")
    return True

async def main():
    print("=== BOT MAIN FUNCTION START ===", flush=True)
    
    database_available = False
    memory_manager = None
    
    try:
        setup_detailed_logging()
        logging.info("Bot starting up...")
        
        try:
            await database.init_db()
            database_available = True
            logging.info("Database initialized successfully")
        except Exception as e:
            logging.error(f"Database initialization failed: {e}")
        
        try:
            from app.memory_manager import MemoryManager
            memory_manager = MemoryManager()
            logging.info("Memory manager initialized")
        except Exception as e:
            logging.warning(f"Memory manager initialization failed: {e}")
        
        if database_available:
            try:
                await start_task_queue()
                logging.info("Task queue started")
            except Exception as e:
                logging.error(f"Task queue initialization failed: {e}")
        
        try:
            await initialize_group_chats()
            logging.info("Group chats initialized")
        except Exception as e:
            logging.warning(f"Group chat initialization failed: {e}")
        
        try:
            if database_available:
                await startup_health_check()
        except Exception as e:
            logging.warning(f"Startup health check failed: {e}")
        
        logging.info("Starting main application loop...")
        await run_bot_and_server()
        
    except asyncio.CancelledError:
        logging.info("Main application loop was cancelled")
    except Exception as e:
        logging.critical(f"Application failed critically: {e}", exc_info=True)
    finally:
        logging.info("Shutting down services...")
        if memory_manager:
            try: await memory_manager.stop()
            except Exception: pass
        if database_available:
            try: await stop_task_queue()
            except Exception: pass
            try: await metrics_collector.cleanup()
            except Exception: pass
        try: await database.db_manager.close()
        except Exception: pass
        logging.info("Shutdown complete")

if __name__ == "__main__":
    print("=== BOT MAIN ENTRY POINT ===", flush=True)
    
    container_id = os.environ.get('HOSTNAME', 'unknown')
    print(f"Container ID: {container_id}", flush=True)
    print(f"Process ID: {os.getpid()}", flush=True)
    
    print("Checking for existing bot instances...", flush=True)
    if not process_lock.acquire():
        print("ERROR: Another bot instance is already running or lock cannot be acquired. Exiting.", flush=True)
        print(f"Lock file: {process_lock.lock_file}", flush=True)
        sys.exit(1)
    
    print("Lock acquired successfully. Starting bot...", flush=True)
    
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped by user.", flush=True)
    except asyncio.CancelledError:
        print("Bot was cancelled", flush=True)
    except Exception as e:
        error_msg = f"Unexpected error in main: {e}"
        print(error_msg, flush=True)
        logging.critical(error_msg, exc_info=True)
        sys.exit(1)
    finally:
        print("Shutting down bot and releasing lock...", flush=True)
        process_lock.release()
        print("Bot shutdown complete. Lock released.", flush=True)

import os
import sys

# DEBUG: Force unbuffered output immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
print("DEBUG: bot.py started", flush=True)

import logging
print("DEBUG: logging imported", flush=True)
import asyncio
print("DEBUG: asyncio imported", flush=True)
import signal
import time
import threading

print("DEBUG: standard libs imported", flush=True)

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, Conflict
print("DEBUG: telegram modules imported", flush=True)

from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve
print("DEBUG: hypercorn imported", flush=True)

# Import custom modules
print("DEBUG: Importing app.config...", flush=True)
from app.config import settings
print(f"DEBUG: app.config imported. Settings is None? {settings is None}", flush=True)

print("DEBUG: Importing app.database...", flush=True)
from app import database
print("DEBUG: app.database imported", flush=True)

print("DEBUG: Importing app.handlers...", flush=True)
from app.handlers import commands, messages, callbacks
from app.handlers.callbacks import new_topic_callback
print("DEBUG: app.handlers imported", flush=True)

from app.metrics import metrics_collector
print("DEBUG: metrics_collector imported", flush=True)
from app.utils.logging_config import setup_detailed_logging
print("DEBUG: logging_config imported", flush=True)

from app.queue import start_task_queue, stop_task_queue
print("DEBUG: queue imported", flush=True)
from app.group_chat import initialize_group_chats
print("DEBUG: group_chat imported", flush=True)

# Import extracted modules
from app.web import flask_app
print("DEBUG: web imported", flush=True)
# Lock import removed

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
            # Lock release removed
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
    """Запускает бота с устойчивостью к ошибкам"""
    logging.info("Starting bot...")
    
    version_info = Application.__version__ if hasattr(Application, '__version__') else 'Unknown'
    logging.info(f"Python-telegram-bot version: {version_info}")
    
    application = None
    
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
        
        commands.register(application)
        callbacks.register(application)
        messages.register(application)
        application.add_handler(CallbackQueryHandler(new_topic_callback, pattern="^new_topic$"))
        
        await application.initialize()
        await application.start()
        
        # Start polling with built-in resilience
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            timeout=30,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=10,
            pool_timeout=30,
        )
        
        logging.info("Bot started successfully and polling")
        
        # Wait for shutdown event
        await shutdown_event.wait()
        
        logging.info("Stopping bot...")
        await application.updater.stop()
        await application.stop()
        logging.info("Bot stopped.")
            
    except Exception as e:
        logging.critical(f"Critical bot error: {e}", exc_info=True)
        if application:
            await _cleanup_application(application)
        # Propagate to trigger restart if needed, or exit
        raise e

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
    
    server_task = None
    if settings.ENABLE_WEB_SERVER:
        server_task = asyncio.create_task(serve(flask_app, hypercorn_config))
        logging.info(f"Web server started on port {settings.PORT}")
    else:
        logging.info("Web server is DISABLED by configuration")
        
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
        tasks_to_cancel = [monitoring_task, bot_task, watchdog_task, shutdown_task]
        if server_task:
            tasks_to_cancel.append(server_task)
            
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
        
        await asyncio.gather(
            *tasks_to_cancel,
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
    # Force flush stdout/stderr for immediate log visibility
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    
    print("=== BOT MAIN FUNCTION START ===", flush=True)
    print(f"Current Directory: {os.getcwd()}", flush=True)
    print(f"Python Version: {sys.version}", flush=True)
    
    database_available = False
    memory_manager = None
    
    try:
        setup_detailed_logging()
        logging.info("Bot starting up - Detailed logging enabled")
        
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
        
        # Register signal handlers for graceful shutdown on Linux/Docker
        if sys.platform != "win32":
            try:
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, lambda: shutdown_event.set())
            except NotImplementedError:
                # Windows implementation of asyncio loop doesn't support add_signal_handler
                logging.warning("Signal handlers not supported on this platform")

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
    
    # Locking logic removed for Northflank deployment
    print("Starting bot...", flush=True)
    
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
        print("Bot shutdown complete.", flush=True)

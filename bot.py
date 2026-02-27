# ruff: noqa: E402
import os
import sys

# Force unbuffered output for container log visibility
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import logging
import asyncio
import signal
import time
import threading

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes


async def global_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log the error and send a telegram message to notify the user."""
    logging.error(
        f"Exception while handling an update: {context.error}", exc_info=context.error
    )

    # Send message to user if possible
    if isinstance(update, Update) and update.effective_message:
        try:
            # Avoid infinite loops if error happens during sending error message
            text = "❌ Произошла непредвиденная ошибка. Попробуйте позже."
            await update.effective_message.reply_text(text)
        except Exception:
            pass


from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve

# Import custom modules
from app.config import settings
from app import database
from app.handlers import commands, messages, callbacks
from app.handlers.callbacks import new_topic_callback
from app.metrics import metrics_collector
from app.utils.logging_config import setup_detailed_logging
from app.queue import start_task_queue, stop_task_queue
from app.group_chat import initialize_group_chats

# Import extracted modules
from app.web import flask_app

# Global shutdown event
shutdown_event = asyncio.Event()


def signal_handler(signum, _frame):
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
                        logging.info(
                            f"Metrics summary: {metrics_summary['total_requests']} requests, {metrics_summary['error_rate']:.1f}% errors"
                        )
                    else:
                        logging.warning(
                            "Metrics/Queue stats unavailable - database not accessible"
                        )

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
        if hasattr(application, "updater") and application.updater:
            await application.updater.stop()
    except Exception as cleanup_error:
        logging.warning(f"Cleanup error (updater): {cleanup_error}")

    try:
        if hasattr(application, "_initialized") and application._initialized:
            await application.stop()
    except Exception as cleanup_error:
        logging.warning(f"Cleanup error (application): {cleanup_error}")


async def run_bot_with_retry():
    """Запускает бота с устойчивостью к ошибкам"""
    logging.info("Starting bot...")

    version_info = (
        Application.__version__ if hasattr(Application, "__version__") else "Unknown"
    )
    logging.info(f"Python-telegram-bot version: {version_info}")

    application = None

    try:
        from telegram.request import HTTPXRequest

        custom_request = HTTPXRequest(
            connection_pool_size=50,
            connect_timeout=10.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=60.0,
        )
        application = (
            Application.builder()
            .token(settings.TELEGRAM_BOT_TOKEN)
            .request(custom_request)
            .concurrent_updates(True)
            .build()
        )
        commands.register(application)
        callbacks.register(application)
        messages.register(application)
        application.add_handler(
            CallbackQueryHandler(new_topic_callback, pattern="^new_topic$")
        )

        # Register global error handler
        application.add_error_handler(global_error_handler)

        await application.initialize()
        await application.start()

        # Start polling with built-in resilience
        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            timeout=30,
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
            if shutdown_event.is_set():
                break

            check_counter += 1

            if bot_task.done():
                if bot_task.exception():
                    logging.error(
                        f"Bot task failed with exception: {bot_task.exception()}"
                    )
                else:
                    logging.warning("Bot task completed unexpectedly")
            else:
                logging.info("Bot task is running normally")

        except Exception as e:
            logging.error(f"Watchdog error: {e}")
            await asyncio.sleep(30)

    logging.info("Bot watchdog stopped due to shutdown signal")


def _handle_polling_conflict(error: Exception) -> None:
    """Логирует конфликт long polling без побочных эффектов."""
    is_conflict_error = "Conflict" in str(
        error
    ) or "terminated by other getUpdates request" in str(error)

    if is_conflict_error:
        logging.warning(
            "Telegram polling conflict detected: another bot instance might be running."
        )


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
        logging.error(f"Critical error in main loop: {e}", exc_info=True)
        _handle_polling_conflict(e)
    finally:
        logging.info("Starting graceful shutdown...")
        tasks_to_cancel = [monitoring_task, bot_task, watchdog_task, shutdown_task]
        if server_task:
            tasks_to_cancel.append(server_task)

        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        # Resource cleanup (task queue, metrics, DB) is handled by main()'s
        # finally-block to avoid double-free.
        logging.info("Async tasks cancelled; returning to main() for resource cleanup.")


async def _cleanup_with_retry(resource_name, cleanup_coro, retries=1, base_delay=0.25):
    """Safely cleanup a resource with bounded retry/backoff and without breaking shutdown."""
    for attempt in range(1, retries + 1):
        try:
            await cleanup_coro()
            logging.info(
                f"Cleanup successful for {resource_name} (attempt {attempt}/{retries})"
            )
            return
        except Exception as e:
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                logging.warning(
                    f"Cleanup failed for {resource_name} (attempt {attempt}/{retries}): {e}. "
                    f"Retrying in {delay:.2f}s"
                )
                await asyncio.sleep(delay)
            else:
                logging.warning(
                    f"Cleanup failed for {resource_name} after {retries} attempts: {e}"
                )


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
            response = await client.get(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getMe"
            )
            if response.status_code == 200:
                bot_info = response.json()
                if bot_info.get("ok"):
                    logging.info(
                        f"✓ Telegram API verified - Bot: {bot_info['result']['username']}"
                    )
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
    # Quart is ASGI-native; no event loop injection needed.

    database_available = False
    memory_manager = None

    try:
        # Auto-enable structured JSON logging in production containers
        # Supports: STRUCTURED_LOGGING=1, LOG_FORMAT=json, or auto-detect from DATABASE_URL
        _structured = os.environ.get("STRUCTURED_LOGGING", "").lower()
        _log_format = os.environ.get("LOG_FORMAT", "").lower()
        if _structured in ("1", "true", "yes") or _log_format == "json":
            _use_json = True
        elif _structured in ("0", "false", "no") or _log_format == "text":
            _use_json = False
        else:
            # Auto-detect: production has DATABASE_URL set
            _use_json = bool(os.environ.get("DATABASE_URL"))

        setup_detailed_logging(enable_structured_logging=_use_json)
        logging.info(
            "Bot starting up — %s logging enabled",
            "structured JSON" if _use_json else "text",
        )

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
            await _cleanup_with_retry(
                "memory manager",
                memory_manager.stop,
                retries=2,
                base_delay=0.25,
            )
        if database_available:
            await _cleanup_with_retry(
                "task queue",
                stop_task_queue,
                retries=3,
                base_delay=0.5,
            )
            await _cleanup_with_retry(
                "metrics collector",
                metrics_collector.cleanup,
                retries=3,
                base_delay=0.5,
            )
        await _cleanup_with_retry(
            "database manager",
            database.db_manager.close,
            retries=3,
            base_delay=0.5,
        )
        logging.info("Shutdown complete")


if __name__ == "__main__":
    container_id = os.environ.get("HOSTNAME", "unknown")
    logging.info("Starting bot... (container=%s, pid=%s)", container_id, os.getpid())

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user.")
    except asyncio.CancelledError:
        logging.info("Bot was cancelled")
    except Exception as e:
        logging.critical("Unexpected error in main: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        logging.info("Bot shutdown complete.")

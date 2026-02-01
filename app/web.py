import os
import datetime
import logging
from flask import Flask
from app import database
from app.config import settings

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
                # Используем asyncio.to_thread для безопасной проверки Redis
                import asyncio
                try:
                    # Создаем временный event loop для проверки Redis
                    temp_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(temp_loop)
                    temp_loop.run_until_complete(asyncio.wait_for(
                        asyncio.to_thread(redis_client.ping),
                        timeout=3.0
                    ))
                    temp_loop.close()
                    redis_status = "connected"
                except Exception:
                    redis_status = "disconnected"
                finally:
                    if temp_loop and not temp_loop.is_closed():
                        temp_loop.close()
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

@flask_app.route('/keys')
def keys_status():
    """Endpoint для просмотра статуса ключей Gemini API"""
    try:
        import asyncio
        from app import database

        # Создаем новый event loop для асинхронных операций
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Получаем статистику ключей
            key_stats = loop.run_until_complete(database.get_gemini_key_usage_stats())

            # Получаем информацию об активных ключах
            active_keys = {}
            for model in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"]:
                active_info = loop.run_until_complete(database.get_active_key_info(model))
                if active_info:
                    active_keys[model] = active_info

            keys_status = {
                "timestamp": str(datetime.datetime.now()),
                "active_keys": active_keys,
                "key_usage_stats": key_stats,
                "cache_info": {
                    "cache_ttl_seconds": 300,
                    "models_cached": list(active_keys.keys())
                }
            }

            return keys_status, 200

        finally:
            loop.close()

    except Exception as e:
        return {
            "error": f"Failed to get keys status: {str(e)}",
            "timestamp": str(datetime.datetime.now())
        }, 500

@flask_app.route('/keys/<model_name>')
def model_keys_status(model_name):
    """Endpoint для просмотра статуса ключей конкретной модели"""
    try:
        import asyncio
        from app import database

        # Создаем новый event loop для асинхронных операций
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Получаем статистику ключей для конкретной модели
            key_stats = loop.run_until_complete(database.get_gemini_key_usage_stats(model_name))

            # Получаем информацию об активном ключе
            active_info = loop.run_until_complete(database.get_active_key_info(model_name))

            model_status = {
                "model": model_name,
                "timestamp": str(datetime.datetime.now()),
                "active_key": active_info,
                "all_keys": key_stats,
                "daily_limit": settings.DAILY_LIMITS.get(model_name, "unlimited")
            }

            return model_status, 200

        finally:
            loop.close()

    except Exception as e:
        return {
            "error": f"Failed to get model keys status: {str(e)}",
            "timestamp": str(datetime.datetime.now())
        }, 500

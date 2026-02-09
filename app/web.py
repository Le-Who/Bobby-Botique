import os
import hmac
import datetime
import logging
from functools import wraps
from flask import Flask, render_template, request, abort
from app import database
from app.config import settings

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
flask_app = Flask(__name__)

@flask_app.after_request
def _secure_headers(response):
    """Add security headers to all responses."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    # CSP: Allow fonts from Google, styles inline (for progress bars), images from self/data.
    # Script-src is 'none' as we don't use JS.
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'none'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:;"
    )
    return response

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Security: Only allow token via header to prevent leakage in logs/history
        token = request.headers.get('X-Auth-Token')

        # Determine the expected secret
        expected_secret = os.environ.get('ADMIN_SECRET')
        if not expected_secret and settings:
            expected_secret = settings.TELEGRAM_BOT_TOKEN

        if not expected_secret:
             logging.error("No authentication secret configured for web endpoints.")
             abort(500, description="Server misconfiguration: Authentication secret not set.")

        # Use constant-time comparison to prevent timing attacks
        if not token or not hmac.compare_digest(token, expected_secret):
            abort(401, description="Unauthorized: Invalid or missing token. Use 'X-Auth-Token' header.")

        return f(*args, **kwargs)
    return decorated_function

@flask_app.route('/')
@require_auth
def dashboard():
    """Main Dashboard Endpoint"""
    try:
        # Collect Status Data
        status_data = {
            "bot": "running",
            "database": "connected" if database.db_pool else "disconnected",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "2.1.0",
            "environment": os.getenv("ENVIRONMENT", "production")
        }

        # System Metrics
        import psutil
        try:
            status_data["system"] = {
                "cpu_percent": psutil.cpu_percent(interval=None), # Non-blocking
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            }
        except ImportError:
            status_data["system"] = {"cpu_percent": 0, "memory_percent": 0, "disk_percent": 0}

        return render_template('status.html', status=status_data)
    except Exception as e:
        logging.error(f"Dashboard error: {e}", exc_info=True)
        return "Internal Server Error", 500

@flask_app.route('/status') # Keep JSON API for automated monitoring
@require_auth
def status_api():
    """JSON Status API for external monitoring tools"""
    try:
        # Reusing logic for JSON response...
        status = {
            "bot": "running",
            "database": "connected" if database.db_pool else "disconnected",
            "timestamp": str(datetime.datetime.now()),
            "system": {}
        }
        import psutil
        try:
             status["system"] = {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            }
        except: pass
        return status, 200
    except Exception as e:
        logging.error(f"Status API error: {e}", exc_info=True)
        return {"error": "Internal Server Error"}, 500


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
            # Removed sensitive info (container_id, process_id)
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
        logging.error(f"Health check error: {e}", exc_info=True)
        return {
            "status": "unhealthy",
            "error": "Internal Server Error",
            "timestamp": str(datetime.datetime.now())
        }, 500

@flask_app.route('/keys')
@require_auth
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
        logging.error(f"Keys status error: {e}", exc_info=True)
        return {
            "error": "Internal Server Error",
            "timestamp": str(datetime.datetime.now())
        }, 500

@flask_app.route('/keys/<model_name>')
@require_auth
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
        logging.error(f"Model keys status error: {e}", exc_info=True)
        return {
            "error": "Internal Server Error",
            "timestamp": str(datetime.datetime.now())
        }, 500

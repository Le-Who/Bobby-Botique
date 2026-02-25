"""
Web dashboard for GemAI Bot v2.

Provides:
- Cookie-session login (browser-friendly)
- Comprehensive metrics dashboard
- JSON API endpoints for operational data
- Health check (unauthenticated) for Northflank
"""

import os
import hmac
import asyncio
import hashlib
import datetime
import logging
import time
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    abort,
    jsonify,
)
from app import database
from app.config import settings

# --- FLASK APP SETUP ---
flask_app = Flask(__name__)

# Derive a secret key for Flask sessions from ADMIN_SECRET
_admin_secret = os.environ.get("ADMIN_SECRET", "")
if not _admin_secret and settings:
    _admin_secret = getattr(settings, "ADMIN_SECRET", "") or ""
flask_app.secret_key = hashlib.sha256(
    f"gemaibotv2-session-{_admin_secret}".encode()
).hexdigest()

# Session configuration
flask_app.config["SESSION_COOKIE_NAME"] = "gembot_session"
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=7)


# =============================================================================
# SECURITY HEADERS
# =============================================================================


@flask_app.after_request
def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# =============================================================================
# AUTHENTICATION
# =============================================================================


def _get_admin_secret():
    """Returns the configured admin secret."""
    secret = os.environ.get("ADMIN_SECRET")
    if not secret and settings:
        secret = getattr(settings, "ADMIN_SECRET", None)
    return secret


def _is_authenticated():
    """Check if current request has a valid session or header token."""
    # Check session cookie first
    if session.get("authenticated"):
        return True
    # Fallback: check X-Auth-Token header (for API/monitoring tools)
    token = request.headers.get("X-Auth-Token")
    expected = _get_admin_secret()
    if token and expected and hmac.compare_digest(token, expected):
        return True
    return False


def require_auth(f):
    """Decorator that requires authentication via session cookie or header token."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _is_authenticated():
            # For API endpoints, return 401 JSON
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            # For pages, redirect to login
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)

    return decorated_function


@flask_app.route("/login", methods=["GET", "POST"])
def login_page():
    """Login page with password form."""
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        expected = _get_admin_secret()

        if not expected:
            error = "Server misconfiguration: ADMIN_SECRET not set."
        elif hmac.compare_digest(password, expected):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid password."

    return render_template("login.html", error=error)


@flask_app.route("/logout")
def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for("login_page"))


# =============================================================================
# DASHBOARD PAGE
# =============================================================================


@flask_app.route("/")
@require_auth
def dashboard():
    """Main Dashboard — serves the HTML shell. Data loaded via JS fetch."""
    try:
        return render_template("dashboard.html")
    except Exception as e:
        logging.error("Dashboard error: %s", e, exc_info=True)
        return "Internal Server Error", 500


# =============================================================================
# HEALTH CHECK (UNAUTHENTICATED — for Northflank)
# =============================================================================


@flask_app.route("/health")
def health_check_endpoint():
    """Health check endpoint for Northflank monitoring."""
    try:
        db_ok = database.is_database_connected()
        overall = "healthy" if db_ok else "unhealthy"

        health = {
            "status": overall,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "service": "gemaibotv2",
            "services": {
                "bot": "running",
                "database": "connected" if db_ok else "disconnected",
            },
        }

        # Check Redis without blocking
        try:
            from app.cache import redis_client

            if redis_client:
                try:
                    redis_client.ping()
                    health["services"]["redis"] = "connected"
                except Exception:
                    health["services"]["redis"] = "disconnected"
            else:
                health["services"]["redis"] = "not_configured"
        except Exception:
            health["services"]["redis"] = "unknown"

        return jsonify(health), 200 if overall == "healthy" else 503

    except Exception as e:
        logging.error("Health check error: %s", e, exc_info=True)
        return jsonify({"status": "unhealthy", "error": str(type(e).__name__)}), 500


# =============================================================================
# API ENDPOINTS — JSON data for dashboard
# =============================================================================


# Reference to the main asyncio event loop (set during startup in bot.py).
# Needed because Flask routes run in Hypercorn's worker threads which have
# no event loop; we schedule async work back onto the main loop.
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Store the main event loop so _run_async can schedule onto it."""
    global _main_loop
    _main_loop = loop


def _run_async(coro):
    """Run an async coroutine from sync Flask context.

    Hypercorn serves Flask WSGI handlers inside worker threads that do NOT
    have their own event loop.  The asyncpg connection pool (and its internal
    Futures) is bound to the main event loop, so we must schedule the
    coroutine there — never create a second loop.
    """
    # Fast path: if the main loop reference was captured at startup, use it.
    if _main_loop is not None and _main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, _main_loop)
        return future.result(timeout=15)

    # Fallback: no Hypercorn (e.g. unit tests, local `flask run`).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=15)
    else:
        return asyncio.run(coro)


@flask_app.route("/api/overview")
@require_auth
def api_overview():
    """High-level system overview: system health, bot uptime, key counts."""
    try:
        import psutil

        system = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "memory_used_mb": round(
                psutil.virtual_memory().used / (1024 * 1024), 1
            ),
            "memory_total_mb": round(
                psutil.virtual_memory().total / (1024 * 1024), 1
            ),
            "disk_percent": psutil.disk_usage("/").percent,
        }
    except Exception:
        system = {
            "cpu_percent": 0,
            "memory_percent": 0,
            "memory_used_mb": 0,
            "memory_total_mb": 0,
            "disk_percent": 0,
        }

    # Metrics summary
    try:
        from app.metrics import metrics_collector

        metrics = _run_async(metrics_collector.get_metrics_summary())
    except Exception:
        metrics = {}

    # Database status
    db_status = database.is_database_connected()

    # Redis status
    try:
        from app.cache import redis_client

        redis_ok = bool(redis_client and redis_client.ping())
    except Exception:
        redis_ok = False

    return jsonify(
        {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "system": system,
            "metrics": metrics,
            "services": {
                "database": "connected" if db_status else "disconnected",
                "redis": "connected" if redis_ok else "disconnected",
                "bot": "running",
            },
        }
    )


@flask_app.route("/api/keys")
@require_auth
def api_keys():
    """API key usage statistics for all models."""
    try:
        key_stats = _run_async(database.get_gemini_key_usage_stats())

        # Get active keys per model
        active_keys = {}
        models = settings.AVAILABLE_MODELS or []

        for model in models:
            try:
                info = _run_async(database.get_active_key_info(model))
                if info:
                    active_keys[model] = info
            except Exception:
                pass

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "key_usage": key_stats,
                "active_keys": active_keys,
                "daily_limits": getattr(settings, "DAILY_LIMITS", {}),
            }
        )
    except Exception as e:
        logging.error("API keys error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/errors")
@require_auth
def api_errors():
    """Recent errors from metrics collector."""
    try:
        from app.metrics import metrics_collector

        summary = _run_async(metrics_collector.get_metrics_summary())
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "error_count": summary.get("error_count", 0),
                "error_rate": summary.get("error_rate_percent", 0),
                "recent_errors": getattr(
                    metrics_collector, "_recent_errors", []
                ),
            }
        )
    except Exception as e:
        logging.error("API errors error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/cache")
@require_auth
def api_cache():
    """Cache performance statistics."""
    try:
        from app.cache import get_cache_stats, get_multi_layer_cache_stats

        redis_stats = _run_async(get_cache_stats())
        ml_stats = _run_async(get_multi_layer_cache_stats())

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "redis": redis_stats,
                "multi_layer": ml_stats,
            }
        )
    except Exception as e:
        logging.error("API cache error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/queue")
@require_auth
def api_queue():
    """Task queue statistics."""
    try:
        from app.queue import task_queue

        stats = _run_async(task_queue.get_queue_stats())
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "queue": stats,
            }
        )
    except Exception as e:
        logging.error("API queue error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/database")
@require_auth
def api_database():
    """Database connection pool and health stats."""
    try:
        db_metrics = _run_async(database.get_supabase_metrics())
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "database": db_metrics,
            }
        )
    except Exception as e:
        logging.error("API database error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/circuit-breakers")
@require_auth
def api_circuit_breakers():
    """Circuit breaker states."""
    try:
        from app.circuit_breaker import _circuit_breakers

        breakers = {}
        for name, cb in _circuit_breakers.items():
            breakers[name] = cb.get_stats()

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "circuit_breakers": breakers,
            }
        )
    except Exception as e:
        logging.error("API circuit breakers error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500


@flask_app.route("/api/memory")
@require_auth
def api_memory():
    """Memory manager statistics."""
    try:
        from app.memory_manager import get_memory_stats

        stats = get_memory_stats()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "memory": stats,
            }
        )
    except Exception as e:
        logging.error("API memory error: %s", e, exc_info=True)
        return jsonify({"error": str(type(e).__name__)}), 500

"""
Web dashboard for GemAI Bot v2.

Provides:
- Cookie-session login (browser-friendly)
- Comprehensive metrics dashboard
- JSON API endpoints for operational data
- Health check (unauthenticated) for Northflank

Uses Quart (ASGI-native Flask-compatible framework) served directly by
Hypercorn — no sync↔async bridge needed.
"""

import asyncio
import datetime
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict
from functools import wraps

from quart import (
    Quart,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import database
from app.config import settings
from app.repos.metrics_repo import (
    get_active_key_info,
    get_gemini_key_usage_stats,
    get_supabase_metrics,
    get_tavily_key_usage_stats,
)

# --- QUART APP SETUP ---
flask_app = Quart(__name__)  # kept as `flask_app` for backward compat with bot.py


# Derive a secret key for sessions from ADMIN_SECRET
def _get_admin_secret():
    """Returns the configured admin secret."""
    return getattr(settings, "ADMIN_SECRET", None) or os.environ.get("ADMIN_SECRET")


# Placeholder — overridden at server startup via @before_serving
flask_app.secret_key = os.urandom(32)  # Secure fallback — overridden at startup


@flask_app.before_serving
async def _init_secret_key():
    """Compute session secret_key at startup when ADMIN_SECRET is definitely available."""
    admin_secret = _get_admin_secret() or ""
    flask_app.secret_key = hashlib.sha256(
        f"gemaibotv2-session-{admin_secret}".encode()
    ).hexdigest()
    if not admin_secret:
        logging.warning("ADMIN_SECRET is empty — session secret_key is weak")

# Session configuration
flask_app.config["SESSION_COOKIE_NAME"] = "gembot_session"
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
flask_app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("DATABASE_URL"))  # True in production
flask_app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=7)


# =============================================================================
# SECURITY HEADERS
# =============================================================================


@flask_app.before_request
async def generate_csp_nonce():
    """Generate a per-request CSP nonce for inline scripts/styles."""
    g.csp_nonce = secrets.token_urlsafe(16)


@flask_app.after_request
async def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    nonce = getattr(g, "csp_nonce", "")
    csp = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
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

# _get_admin_secret() is defined above (before session key derivation).


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
    async def decorated_function(*args, **kwargs):
        if not _is_authenticated():
            # For API endpoints, return 401 JSON
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            # For pages, redirect to login
            return redirect(url_for("login_page"))
        return await f(*args, **kwargs)

    return decorated_function


# Simple IP-based login rate limiter (brute-force protection)
_login_attempts: dict[str, list] = defaultdict(list)
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_login_cleanup_counter = 0


def _check_login_rate_limit(ip: str) -> bool:
    """Returns True if allowed, False if rate limited."""
    global _login_cleanup_counter
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]

    # Periodic cleanup: evict stale IPs every 50 checks
    _login_cleanup_counter += 1
    if _login_cleanup_counter >= 50:
        _login_cleanup_counter = 0
        stale = [k for k, v in _login_attempts.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del _login_attempts[k]

    if len(_login_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        return False
    return True


def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


@flask_app.route("/login", methods=["GET", "POST"])
async def login_page():
    """Login page with password form, CSRF protection, and brute-force rate limiting."""
    error = None

    # Securely extract client IP behind proxy (Northflank)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Use rightmost IP from X-Forwarded-For to prevent spoofing
        client_ip = forwarded_for.split(",")[-1].strip()
    else:
        client_ip = request.remote_addr or "unknown"

    if request.method == "POST":
        # Check brute-force rate limit
        if not _check_login_rate_limit(client_ip):
            logging.warning("Login rate limit exceeded for IP %s", client_ip)
            error = "Слишком много попыток входа. Повторите через 5 минут."
            csrf_token = secrets.token_hex(32)
            session["csrf_token"] = csrf_token
            return await render_template("login.html", error=error, csrf_token=csrf_token), 429

        form = await request.form
        password = form.get("password", "")
        csrf_token = form.get("csrf_token", "")
        expected = _get_admin_secret()

        # Validate CSRF token
        expected_csrf = session.get("csrf_token", "")
        if not csrf_token or not expected_csrf or not hmac.compare_digest(csrf_token, expected_csrf):
            error = "Invalid request. Please try again."
        elif not expected:
            error = "Server misconfiguration: ADMIN_SECRET not set."
        elif hmac.compare_digest(password, expected):
            session.pop("csrf_token", None)  # Consume the token
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            _record_login_attempt(client_ip)
            error = "Invalid password."

    # Generate fresh CSRF token for the form
    csrf_token = secrets.token_hex(32)
    session["csrf_token"] = csrf_token

    return await render_template("login.html", error=error, csrf_token=csrf_token)


@flask_app.route("/logout")
async def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for("login_page"))


# =============================================================================
# DASHBOARD PAGE
# =============================================================================


@flask_app.route("/")
@require_auth
async def dashboard():
    """Main Dashboard — serves the HTML shell. Data loaded via JS fetch."""
    try:
        return await render_template("dashboard.html")
    except Exception as e:
        logging.error("Dashboard error: %s", e, exc_info=True)
        return "Internal Server Error", 500


# =============================================================================
# HEALTH CHECK (UNAUTHENTICATED — for Northflank)
# =============================================================================


@flask_app.route("/health")
async def health_check_endpoint():
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

        # Check Redis without blocking (ping_safe never throws or warns)
        try:
            from app.cache import ping_safe, redis_client

            if redis_client is None:
                health["services"]["redis"] = "not_configured"
            elif await ping_safe():
                health["services"]["redis"] = "connected"
            else:
                health["services"]["redis"] = "disconnected"
        except Exception:
            health["services"]["redis"] = "unknown"

        return jsonify(health), 200 if overall == "healthy" else 503

    except Exception as e:
        logging.error("Health check error: %s", e, exc_info=True)
        return jsonify({"status": "unhealthy", "error": "internal_error"}), 500


# =============================================================================
# API ENDPOINTS — JSON data for dashboard (native async, no bridge needed)
# =============================================================================


@flask_app.route("/api/overview")
@require_auth
async def api_overview():
    """High-level system overview: system health, bot uptime, key counts."""
    try:
        import psutil

        system = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
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

        metrics = await metrics_collector.get_metrics_summary()
    except Exception:
        metrics = {}

    # Database status
    db_status = database.is_database_connected()

    # Redis status (ping_safe never throws or warns)
    try:
        from app.cache import ping_safe

        redis_ok = await ping_safe()
    except Exception:
        redis_ok = False

    # Summarization metrics
    summarization = {}
    try:
        from app.metrics import role_conv_metrics

        rcm = await role_conv_metrics.get_metrics_summary()
        summarization = rcm.get("summarization", {})
    except Exception:
        pass

    return jsonify(
        {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "system": system,
            "metrics": metrics,
            "summarization": summarization,
            "services": {
                "database": "connected" if db_status else "disconnected",
                "redis": "connected" if redis_ok else "disconnected",
                "bot": "running",
            },
        }
    )


@flask_app.route("/api/keys")
@require_auth
async def api_keys():
    """API key usage statistics for all models."""
    try:
        from app.utils.time import get_kyiv_reset_time

        key_stats = await get_gemini_key_usage_stats()

        # Get Tavily key stats
        tavily_stats = []
        try:
            tavily_stats = await get_tavily_key_usage_stats()
        except Exception:
            pass

        # Get active keys per model (batched to avoid N+1)
        active_keys = {}
        models = settings.AVAILABLE_MODELS or []

        if models:
            results = await asyncio.gather(
                *[get_active_key_info(m) for m in models],
                return_exceptions=True,
            )
            for model, result in zip(models, results, strict=False):
                if isinstance(result, Exception):
                    continue
                if result:
                    active_keys[model] = result

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "key_usage": key_stats,
                "tavily_usage": tavily_stats,
                "active_keys": active_keys,
                "daily_limits": getattr(settings, "DAILY_LIMITS", {}),
                "reset_info": {
                    "gemini_resets": get_kyiv_reset_time(),
                    "tavily_credit_limit": settings.TAVILY_MONTHLY_CREDIT_LIMIT,
                },
            }
        )
    except Exception as e:
        logging.error("API keys error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/errors")
@require_auth
async def api_errors():
    """Recent errors from metrics collector."""
    try:
        from app.metrics import metrics_collector

        summary = await metrics_collector.get_metrics_summary()
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
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/cache")
@require_auth
async def api_cache():
    """Cache performance statistics."""
    try:
        from app.cache import get_multi_layer_cache_stats

        ml_stats = await get_multi_layer_cache_stats()

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "redis": ml_stats.get("redis", {}),
                "multi_layer": ml_stats,
            }
        )
    except Exception as e:
        logging.error("API cache error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/queue")
@require_auth
async def api_queue():
    """Task queue statistics."""
    try:
        from app.queue import task_queue

        stats = await task_queue.get_queue_stats()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "queue": stats,
            }
        )
    except Exception as e:
        logging.error("API queue error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/database")
@require_auth
async def api_database():
    """Database connection pool and health stats."""
    try:
        db_metrics = await get_supabase_metrics()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "database": db_metrics,
            }
        )
    except Exception as e:
        logging.error("API database error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/circuit-breakers")
@require_auth
async def api_circuit_breakers():
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
        return jsonify({"error": "internal_error"}), 500


@flask_app.route("/api/memory")
@require_auth
async def api_memory():
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
        return jsonify({"error": "internal_error"}), 500

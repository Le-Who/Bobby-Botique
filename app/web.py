"""
Web dashboard for GemAI Bot v2.

Provides:
- Cookie-session login (browser-friendly)
- Comprehensive metrics dashboard
- JSON API endpoints for operational data
- Health check (unauthenticated) for Docker

Uses Quart (ASGI-native Flask-compatible framework) served directly by
Hypercorn — no sync↔async bridge needed.
"""

import asyncio
import contextlib
import datetime
import hashlib
import hmac
import logging
import os
import secrets
import time
import weakref
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
from app.dashboard_sources import DashboardSourceCache, bot_lifecycle
from app.games import daily_2048 as daily_2048_game
from app.observability.context import current_context, request_scope
from app.observability.events import emit, record_exception
from app.observability.ingress import sanitize_client_request_id
from app.repos import daily_2048 as daily_2048_repo
from app.repos.metrics_repo import (
    get_active_key_info,
    get_gemini_key_usage_stats,
    get_supabase_metrics,
    get_tavily_key_usage_stats,
)
from app.repos.settings_repo import set_global_setting
from app.utils.json_compat import json

# --- QUART APP SETUP ---
quart_app = Quart(__name__)  # kept as `quart_app` for backward compat with bot.py

# Register Telegram Mini App blueprint
from app.web_miniapp import miniapp_blueprint  # noqa: E402

quart_app.register_blueprint(miniapp_blueprint, url_prefix="/webapp")

from app.web_natal import natal_bp  # noqa: E402

quart_app.register_blueprint(natal_bp)

from app.web_compatibility import compatibility_bp  # noqa: E402

quart_app.register_blueprint(compatibility_bp, url_prefix="/webapp")


# Derive a secret key for sessions from ADMIN_SECRET
def _get_admin_secret():
    """Returns the configured admin secret."""
    return getattr(settings, "ADMIN_SECRET", None) or os.environ.get("ADMIN_SECRET")


# Placeholder — overridden at server startup via @before_serving
quart_app.secret_key = os.urandom(32)  # Secure fallback — overridden at startup


@quart_app.before_serving
async def _init_secret_key():
    """Compute session secret_key at startup when ADMIN_SECRET is definitely available."""
    admin_secret = _get_admin_secret()
    if admin_secret:
        quart_app.secret_key = hashlib.sha256(f"gemaibotv2-session-{admin_secret}".encode()).hexdigest()
    else:
        quart_app.secret_key = os.urandom(32)
        logging.warning("ADMIN_SECRET is empty — dashboard password and session authentication are disabled")


# Session configuration
quart_app.config["SESSION_COOKIE_NAME"] = "gembot_session"
quart_app.config["SESSION_COOKIE_HTTPONLY"] = True
quart_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
quart_app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("DATABASE_URL"))  # True in production
quart_app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(days=7)


# =============================================================================
# SECURITY HEADERS
# =============================================================================


@quart_app.before_request
async def start_observability_request():
    """Create a server-owned HTTP correlation scope before endpoint work."""
    client_request_id = sanitize_client_request_id(request.headers.get("X-Request-ID"))
    scope = request_scope(
        operation="http.request",
        client_request_id=client_request_id,
    )
    scope.__enter__()
    g.observability_scope = scope
    g.observability_started_ns = time.monotonic_ns()
    g.observability_finished = False
    emit(
        "http.request_started",
        operation="http.request",
        method=request.method,
        endpoint=request.endpoint,
        client_request_id_present=client_request_id is not None,
    )


@quart_app.before_request
async def generate_csp_nonce():
    """Generate a per-request CSP nonce for inline scripts/styles."""
    g.csp_nonce = secrets.token_urlsafe(16)


@quart_app.after_request
async def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["X-Request-ID"] = current_context().request_id or ""

    started_ns = getattr(g, "observability_started_ns", time.monotonic_ns())
    emit(
        "http.request_finished",
        level="warning" if response.status_code >= 500 else "info",
        operation="http.request",
        outcome="failed" if response.status_code >= 500 else "succeeded",
        method=request.method,
        endpoint=request.endpoint,
        status_code=response.status_code,
        duration_ms=round((time.monotonic_ns() - started_ns) / 1_000_000, 2),
    )
    g.observability_finished = True

    nonce = getattr(g, "csp_nonce", "")
    is_natal_report = request.path.startswith("/reports/natal/")
    is_health_check = request.path == "/health"
    is_webapp = request.path.startswith("/webapp")

    if is_natal_report:
        response.headers["Referrer-Policy"] = "no-referrer"
        csp = response.headers.get("Content-Security-Policy") or (
            "default-src 'self'; "
            "script-src 'none'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Frame-Options"] = "DENY"
    elif is_health_check:
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Frame-Options"] = "DENY"
    elif is_webapp:
        # Telegram Mini App: allow telegram.org SDK script, inline styles,
        # and framing by Telegram's WebView
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org 'unsafe-inline' "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' wss:; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org;"
        )
        # Allow Telegram to embed this page
        response.headers["X-Frame-Options"] = "ALLOWALL"
    else:
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Frame-Options"] = "DENY"

    response.headers["Content-Security-Policy"] = csp
    return response


@quart_app.teardown_request
async def finish_observability_request(error: BaseException | None) -> None:
    """Record exceptional exits and always reset the HTTP ContextVar token."""
    scope = getattr(g, "observability_scope", None)
    try:
        if error is not None and not getattr(g, "observability_finished", False):
            started_ns = getattr(g, "observability_started_ns", time.monotonic_ns())
            error_id = record_exception(
                "http.request_failed",
                error,
                operation="http.request",
                fields={
                    "method": request.method,
                    "endpoint": request.endpoint,
                },
            )
            emit(
                "http.request_finished",
                level="error",
                operation="http.request",
                outcome="failed",
                method=request.method,
                endpoint=request.endpoint,
                duration_ms=round((time.monotonic_ns() - started_ns) / 1_000_000, 2),
                error_id=error_id,
            )
    finally:
        if scope is not None:
            scope.__exit__(None, None, None)


# =============================================================================
# AUTHENTICATION
# =============================================================================

# _get_admin_secret() is defined above (before session key derivation).


def _is_authenticated():
    """Check if current request has a valid session, header token, or Telegram admin initData."""
    expected = _get_admin_secret()
    # Check session cookie first
    if expected and session.get("authenticated"):
        return True
    # Fallback: check X-Auth-Token header (for API/monitoring tools)
    token = request.headers.get("X-Auth-Token")
    if token and expected and hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return True
    # Fallback: check Authorization header for Telegram WebApp initData (tma <initData>)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("tma "):
        init_data = auth_header[4:]
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        if bot_token:
            from app.web_miniapp import _extract_user_id, _validate_init_data

            validated = _validate_init_data(init_data, bot_token)
            if validated:
                user_id = _extract_user_id(validated)
                if user_id == getattr(settings, "ADMIN_ID", None):
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


from app.security import SyncRateLimiter, rate_limit  # noqa: E402

_login_limiter = SyncRateLimiter(max_requests=5, window_seconds=300)
_api_limiter = SyncRateLimiter(max_requests=60, window_seconds=60)

# Rate-limit decorator for API endpoints (60 req/min per IP)
rate_limit_api = rate_limit(_api_limiter, use_json=True)

from app.web_controls import register_controls  # noqa: E402

register_controls(quart_app, require_auth)

from app.web_daily_jobs import register_daily_routes  # noqa: E402

register_daily_routes(quart_app, require_auth, rate_limit_api)

from app.web_broadcast_jobs import register_broadcast_routes  # noqa: E402

register_broadcast_routes(quart_app, require_auth, rate_limit_api)


@quart_app.route("/login", methods=["GET", "POST"])
async def login_page():
    """Login page with password form, CSRF protection, and brute-force rate limiting."""
    error = None
    client_ip = request.remote_addr or "unknown"

    if request.method == "POST":
        # Check brute-force rate limit
        if not _login_limiter.check(client_ip):
            logging.warning("Login rate limit exceeded for IP %s", client_ip)
            error = "Слишком много попыток входа. Повторите через 5 минут."
            csrf_token = secrets.token_hex(32)
            session["csrf_token"] = csrf_token
            return (
                await render_template("login.html", error=error, csrf_token=csrf_token),
                429,
            )

        form = await request.form
        password = form.get("password", "")
        csrf_token = form.get("csrf_token", "")
        expected = _get_admin_secret()

        # Validate CSRF token
        expected_csrf = session.get("csrf_token", "")
        if (
            not csrf_token
            or not expected_csrf
            or not hmac.compare_digest(csrf_token.encode("utf-8"), expected_csrf.encode("utf-8"))
        ):
            error = "Invalid request. Please try again."
        elif not expected:
            error = "Server misconfiguration: ADMIN_SECRET not set."
        elif hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
            session.pop("csrf_token", None)  # Consume the token
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            _login_limiter.record(client_ip)
            error = "Invalid password."

    # Generate fresh CSRF token for the form
    csrf_token = secrets.token_hex(32)
    session["csrf_token"] = csrf_token

    return await render_template("login.html", error=error, csrf_token=csrf_token)


@quart_app.route("/logout")
async def logout():
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for("login_page"))


# =============================================================================
# DASHBOARD PAGE
# =============================================================================


@quart_app.route("/")
@require_auth
async def dashboard():
    """Main Dashboard — serves the HTML shell. Data loaded via JS fetch."""
    try:
        return await render_template("dashboard.html")
    except Exception as e:
        logging.error("Dashboard error: %s", e, exc_info=True)
        return "Internal Server Error", 500


# =============================================================================
# HEALTH CHECK (UNAUTHENTICATED — for Docker)
# =============================================================================


@quart_app.route("/health")
async def health_check_endpoint():
    """Health check endpoint for Docker monitoring."""
    try:
        db_ok = database.is_database_connected()
        overall = "healthy" if db_ok else "unhealthy"

        health = {
            "status": overall,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "service": "gemaibotv2",
            "services": {
                "bot": bot_lifecycle.state,
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


@quart_app.route("/metrics")
@rate_limit_api
async def prometheus_metrics():
    """Prometheus text exposition endpoint (unauthenticated for scraping)."""
    try:
        from app.prometheus import generate_metrics_text

        text = generate_metrics_text()
        return text, 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}
    except Exception as e:
        logging.error("Metrics endpoint error: %s", e, exc_info=True)
        return "# error generating metrics\n", 500


# =============================================================================
# API ENDPOINTS — JSON data for dashboard (native async, no bridge needed)
# =============================================================================


@quart_app.route("/api/overview")
@require_auth
@rate_limit_api
async def api_overview():
    """High-level system overview: system health, bot uptime, key counts."""
    try:
        system = await asyncio.to_thread(_sample_system_metrics)
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
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "system": system,
            "metrics": metrics,
            "summarization": summarization,
            "services": {
                "database": "connected" if db_status else "disconnected",
                "redis": "connected" if redis_ok else "disconnected",
                "bot": bot_lifecycle.snapshot()["state"],
            },
        }
    )


@quart_app.route("/api/keys")
@require_auth
@rate_limit_api
async def api_keys():
    """API key usage statistics for all models."""
    try:
        from app.repos.keys import get_tavily_credit_policy
        from app.utils.time import get_kyiv_reset_time

        key_stats = await get_gemini_key_usage_stats()

        # Get Tavily key stats
        tavily_stats = []
        with contextlib.suppress(Exception):
            tavily_stats = await get_tavily_key_usage_stats()

        # Get active keys per model (batched to avoid N+1)
        active_keys = {}
        models = list(dict.fromkeys(row["model_name"] for row in key_stats if row.get("model_name")))

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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "key_usage": key_stats,
                "tavily_usage": tavily_stats,
                "active_keys": active_keys,
                "daily_limits": {
                    **getattr(settings, "DAILY_LIMITS", {}),
                    **{row["model_name"]: row["daily_limit"] for row in key_stats if row.get("model_name")},
                },
                "reset_info": {
                    "gemini_resets": get_kyiv_reset_time(),
                    "tavily_credit_limit": get_tavily_credit_policy()["credit_limit"],
                },
            }
        )
    except Exception as e:
        logging.error("API keys error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/errors")
@require_auth
@rate_limit_api
async def api_errors():
    """Recent errors from metrics collector."""
    try:
        from app.metrics import metrics_collector

        summary = await metrics_collector.get_metrics_summary()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "error_count": summary.get("error_count", 0),
                "error_rate": summary.get("error_rate_percent", 0),
                "recent_errors": summary.get("recent_errors", []),
            }
        )
    except Exception as e:
        logging.error("API errors error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/cache")
@require_auth
@rate_limit_api
async def api_cache():
    """Cache performance statistics."""
    try:
        from app.cache import get_multi_layer_cache_stats

        ml_stats = await get_multi_layer_cache_stats()

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "redis": ml_stats.get("redis", {}),
                "multi_layer": ml_stats,
            }
        )
    except Exception as e:
        logging.error("API cache error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/queue")
@require_auth
@rate_limit_api
async def api_queue():
    """Task queue statistics."""
    try:
        from app.queue import task_queue

        stats = await task_queue.get_queue_stats()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "queue": stats,
            }
        )
    except Exception as e:
        logging.error("API queue error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/database")
@require_auth
@rate_limit_api
async def api_database():
    """Database connection pool and health stats."""
    try:
        db_metrics = await get_supabase_metrics()
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "database": db_metrics,
            }
        )
    except Exception as e:
        logging.error("API database error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/circuit-breakers")
@require_auth
@rate_limit_api
async def api_circuit_breakers():
    """Circuit breaker states."""
    try:
        from app.circuit_breaker import _circuit_breakers

        breakers = {}
        for name, cb in _circuit_breakers.items():
            breakers[name] = cb.get_stats()

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "circuit_breakers": breakers,
            }
        )
    except Exception as e:
        logging.error("API circuit breakers error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


@quart_app.route("/api/memory")
@require_auth
@rate_limit_api
async def api_memory():
    """Memory manager statistics."""
    try:
        from app.memory_manager import get_memory_stats

        stats = await asyncio.to_thread(get_memory_stats)
        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "memory": stats,
            }
        )
    except Exception as e:
        logging.error("API memory error: %s", e, exc_info=True)
        return jsonify({"error": "internal_error"}), 500


# =============================================================================
# BATCH DASHBOARD ENDPOINT — 1 RTT instead of 8
# =============================================================================

_DASHBOARD_SOURCE_TIMEOUT = 5.0


def _sample_system_metrics() -> dict:
    """Sample CPU and host I/O in a worker, away from the HTTP event loop."""
    import psutil

    virtual_memory = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": virtual_memory.percent,
        "memory_used_mb": round(virtual_memory.used / (1024 * 1024), 1),
        "memory_total_mb": round(virtual_memory.total / (1024 * 1024), 1),
        "disk_percent": psutil.disk_usage("/").percent,
    }


async def _safe_fetch(name: str, coro):
    """Run a coroutine and return (name, result) or (name, error_dict)."""
    try:
        # One stalled dependency must not hide healthy sections or leave work
        # running after the browser's ten-second request deadline.
        async with asyncio.timeout(_DASHBOARD_SOURCE_TIMEOUT):
            return name, await coro
    except TimeoutError:
        logging.warning("Dashboard batch: %s timed out", name)
        return name, {"error": "source_timeout"}
    except Exception as e:
        logging.warning("Dashboard batch: %s failed (%s)", name, type(e).__name__)
        return name, {"error": "source_unavailable"}


def _dashboard_source(raw: dict, name: str, default):
    if name not in raw:
        return default, False, "source_unavailable"
    value = raw.get(name, default)
    if isinstance(value, dict) and value.get("error"):
        return default, False, str(value["error"])
    return value, True, None


def _normalize_dashboard_metrics(raw: dict) -> dict:
    average_ms = raw.get("average_response_time_ms")
    if average_ms is None:
        average_ms = float(raw.get("average_response_time", 0) or 0) * 1000
    return {
        "total_requests": int(raw.get("total_requests", raw.get("request_count", 0)) or 0),
        "average_response_time_ms": round(float(average_ms or 0), 2),
        "error_count": int(raw.get("error_count", 0) or 0),
        "error_rate_percent": float(raw.get("error_rate_percent", raw.get("error_rate", 0)) or 0),
        "cache_hit_rate_percent": float(raw.get("cache_hit_rate_percent", raw.get("cache_hit_rate", 0)) or 0),
        "traffic": raw.get("traffic"),
    }


def _assemble_dashboard_snapshot(
    raw: dict,
    *,
    system: dict,
    circuit_breakers: dict,
    generated_at: str | None = None,
) -> dict:
    """Normalize all observability sources into the frontend's sole contract."""
    metrics_raw, metrics_available, metrics_error = _dashboard_source(raw, "metrics", {})
    summarization, summarization_available, summarization_error = _dashboard_source(raw, "summarization", {})
    db_metrics, db_available, db_error = _dashboard_source(raw, "db_metrics", {})
    cache_raw, cache_available, cache_error = _dashboard_source(raw, "cache", {})
    queue_raw, queue_available, queue_error = _dashboard_source(raw, "queue", {})
    gemini_rows, gemini_available, gemini_error = _dashboard_source(raw, "keys_gemini", [])
    tavily_rows, tavily_available, tavily_error = _dashboard_source(raw, "keys_tavily", [])
    key_health, key_health_available, key_health_error = _dashboard_source(raw, "key_health", {})
    redis_ping, redis_available, redis_error = _dashboard_source(raw, "redis_ping", False)

    metrics = _normalize_dashboard_metrics(metrics_raw)
    if "traffic" in raw:
        metrics["traffic"], _, _ = _dashboard_source(raw, "traffic", None)
    recent_errors = list(metrics_raw.get("recent_errors", [])) if isinstance(metrics_raw, dict) else []
    recent_errors.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)

    memory_cache = cache_raw.get("memory", {}) if isinstance(cache_raw, dict) else {}
    redis_cache = cache_raw.get("redis", {}) if isinstance(cache_raw, dict) else {}
    memory_available = redis_stats_available = cache_available
    memory_error = redis_stats_error = cache_error
    if "memory_cache" in raw or "redis_cache" in raw:
        memory_cache, memory_available, memory_error = _dashboard_source(raw, "memory_cache", {})
        redis_cache, redis_stats_available, redis_stats_error = _dashboard_source(raw, "redis_cache", {})
        cache_available = memory_available or redis_stats_available
        cache_error = memory_error or redis_stats_error
    cache = {
        "available": cache_available,
        "error": cache_error,
        "memory_available": memory_available,
        "memory_error": memory_error,
        "redis_available": redis_stats_available,
        "redis_error": redis_stats_error,
        "memory_items": memory_cache.get("memory_items"),
        "memory_capacity": memory_cache.get("memory_max_size"),
        "memory_utilization_percent": memory_cache.get("memory_utilization"),
        "redis_keys": redis_cache.get("total_keys"),
        "redis_used_memory": redis_cache.get("used_memory"),
    }
    queue = {
        "available": queue_available,
        "error": queue_error,
        "pending": queue_raw.get("pending", queue_raw.get("pending_tasks")),
        "running": queue_raw.get("running", queue_raw.get("running_tasks")),
        "completed": queue_raw.get("completed", queue_raw.get("completed_tasks")),
        "failed": queue_raw.get("failed", queue_raw.get("failed_tasks")),
        "workers": queue_raw.get("workers", queue_raw.get("active_workers")),
        "backend": queue_raw.get("backend"),
        "queue_size": queue_raw.get("queue_size"),
        "local_queue_size": queue_raw.get("local_queue_size"),
        "redis_queue_size": queue_raw.get("redis_queue_size"),
        "redis_available": queue_raw.get("redis_available"),
        "execution_scope": queue_raw.get("execution_scope", "process"),
        "partial": queue_raw.get("partial", False),
        "reason_code": queue_raw.get("reason_code"),
        "lanes": queue_raw.get("lanes", {}),
    }

    from app.repos.keys import get_tavily_credit_policy
    from app.utils.time import get_kyiv_reset_time

    return {
        "overview": {
            "available": bool(system) or metrics_available,
            "system": system,
            "metrics": metrics,
            "metrics_available": metrics_available,
            "metrics_error": metrics_error,
            "summarization": summarization,
            "summarization_available": summarization_available,
            "summarization_error": summarization_error,
            "services": {
                "database": db_metrics.get(
                    "status", "connected" if database.is_database_connected() else "disconnected"
                )
                if db_available
                else "unknown",
                "redis": ("connected" if redis_ping else "disconnected") if redis_available else "unknown",
                "bot": bot_lifecycle.snapshot()["state"],
            },
        },
        "providers": {
            "gemini": {"available": gemini_available, "error": gemini_error, "rows": gemini_rows},
            "tavily": {"available": tavily_available, "error": tavily_error, "rows": tavily_rows},
            "reset_info": {
                "gemini_resets": get_kyiv_reset_time(),
                "tavily_credit_limit": get_tavily_credit_policy()["credit_limit"],
            },
            "circuit_breakers": {"available": True, "items": circuit_breakers},
            "key_health": {
                "available": key_health_available,
                "error": key_health_error,
                "summary": key_health,
                "rows": key_health.get("keys", []) if isinstance(key_health, dict) else [],
            },
        },
        "infrastructure": {
            "database": {"available": db_available, "error": db_error, **db_metrics},
            "queue": queue,
            "cache": cache,
            "redis_available": redis_available,
            "redis_error": redis_error,
        },
        "errors": {
            "available": metrics_available,
            "error": metrics_error,
            "count": metrics["error_count"],
            "rate_percent": metrics["error_rate_percent"],
            "recent": recent_errors,
        },
        "generated_at": generated_at or datetime.datetime.now(datetime.UTC).isoformat(),
    }


async def _get_dashboard_key_health() -> dict:
    from app.repos.keys import KeyStatusManager

    manager = KeyStatusManager()
    async with database.db_manager.pool.acquire() as conn, conn.transaction():
        await database.set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            return await manager.get_health_summary(conn=conn)
        finally:
            await database.clear_user_context(conn=conn)


_dashboard_caches: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_DASHBOARD_SOURCE_TTLS = {
    "system": 5,
    "metrics": 15,
    "traffic": 15,
    "summarization": 15,
    "memory_cache": 15,
    "redis_cache": 15,
    "redis_ping": 15,
    "db_metrics": 15,
    "queue": 5,
    "keys_gemini": 15,
    "keys_tavily": 15,
    "key_health": 15,
}


def _get_dashboard_sources() -> DashboardSourceCache:
    from app.cache import get_multi_layer_cache_stats, multi_layer_cache, ping_safe
    from app.dashboard_sources import PartialSourceValue, SourceUnavailable
    from app.metrics import metrics_collector, role_conv_metrics
    from app.queue import task_queue

    loop = asyncio.get_running_loop()
    existing = _dashboard_caches.get(loop)
    if existing is not None:
        return existing

    async def summarization_metrics():
        summary = await role_conv_metrics.get_metrics_summary()
        return summary.get("summarization", {})

    async def system():
        return await asyncio.to_thread(_sample_system_metrics)

    async def process_metrics():
        return await metrics_collector.get_metrics_summary(include_traffic=False)

    async def traffic_metrics():
        value = await metrics_collector.get_traffic_summary()
        periods = value.get("periods", {})
        if not any(period.get("available") is True for period in periods.values()):
            raise SourceUnavailable()
        return value

    async def memory_cache_stats():
        return multi_layer_cache.get_memory_stats()

    async def redis_cache_stats():
        # The existing combined reader catches Redis failures inside this field.
        # Memory has an independent process reader and cannot be blocked by Redis.
        value = (await get_multi_layer_cache_stats()).get("redis")
        if not isinstance(value, dict) or value.get("error"):
            raise SourceUnavailable()
        return value

    async def queue_metrics():
        value = await task_queue.get_queue_stats()
        lanes = value.get("lanes")
        if isinstance(lanes, dict):
            healthy = [item for item in lanes.values() if item.get("available") is True]
            if not healthy:
                raise SourceUnavailable()
            if len(healthy) < len(lanes):
                return PartialSourceValue({**value, "partial": True, "reason_code": "source_partial"})
        elif value.get("available") is False:
            raise SourceUnavailable()
        return value

    def checked(reader):
        async def read():
            value = await reader()
            if isinstance(value, dict) and value.get("error"):
                raise SourceUnavailable()
            return value

        return read

    sources = {
        "system": system,
        "metrics": process_metrics,
        "traffic": traffic_metrics,
        "summarization": summarization_metrics,
        "memory_cache": memory_cache_stats,
        "redis_cache": redis_cache_stats,
        "redis_ping": ping_safe,
        "db_metrics": get_supabase_metrics,
        "queue": queue_metrics,
        "keys_gemini": get_gemini_key_usage_stats,
        "keys_tavily": get_tavily_key_usage_stats,
        "key_health": _get_dashboard_key_health,
    }
    cache = DashboardSourceCache(
        {name: checked(reader) for name, reader in sources.items()},
        timeout_seconds=_DASHBOARD_SOURCE_TIMEOUT,
        source_scopes={
            "metrics": "process",
            "traffic": "mixed",
            "memory_cache": "process",
            "redis_cache": "redis",
            "queue": "mixed",
            "redis_ping": "redis",
            "db_metrics": "database",
            "keys_gemini": "database",
            "keys_tavily": "database",
            "key_health": "database",
        },
    )
    _dashboard_caches[loop] = cache
    return cache


@quart_app.after_serving
async def _close_dashboard_sources() -> None:
    cache = _dashboard_caches.pop(asyncio.get_running_loop(), None)
    if cache is not None:
        await cache.close()


def invalidate_dashboard_sources(*names: str) -> None:
    cache = _dashboard_caches.get(asyncio.get_running_loop())
    if cache is not None:
        for name in names:
            cache.invalidate(name)


@quart_app.after_request
async def _invalidate_dashboard_after_mutation(response):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and 200 <= response.status_code < 300:
        path = request.path
        if any(part in path for part in ("/queue", "/jobs")):
            invalidate_dashboard_sources("queue")
        elif any(part in path for part in ("/models", "/keys", "/prompts", "/runtime-settings", "/admin/controls")):
            invalidate_dashboard_sources("key_health", "keys_gemini", "keys_tavily")
    return response


async def _read_dashboard_sources(names=None) -> tuple[dict, dict]:
    cache = _get_dashboard_sources()
    selected = tuple(_DASHBOARD_SOURCE_TTLS if names is None else names)
    results = await asyncio.gather(
        *(cache.get(name, max_age_seconds=_DASHBOARD_SOURCE_TTLS[name]) for name in selected)
    )
    raw, metadata = {}, {}
    for name, result in zip(selected, results, strict=True):
        raw[name] = result.value if result.available else {"error": result.reason_code}
        metadata[name] = {key: value for key, value in result.to_dict().items() if key != "value"}
    return raw, metadata


async def _build_dashboard_snapshot() -> dict:
    raw, sources = await _read_dashboard_sources()

    system, _, _ = _dashboard_source(raw, "system", {})

    try:
        from app.circuit_breaker import _circuit_breakers

        breakers = {name: breaker.get_stats() for name, breaker in _circuit_breakers.items()}
    except Exception:
        breakers = {}

    snapshot = _assemble_dashboard_snapshot(raw, system=system, circuit_breakers=breakers)
    snapshot["sources"] = sources
    snapshot["overview"]["lifecycle"] = bot_lifecycle.snapshot()
    snapshot["overview"]["traffic_source"] = sources["traffic"]
    snapshot["infrastructure"]["cache"]["memory_source"] = sources["memory_cache"]
    snapshot["infrastructure"]["cache"]["redis_source"] = sources["redis_cache"]
    from app.observability.load_metrics import load_snapshot
    from app.observability.metrics import operational_metrics

    operations = operational_metrics.snapshot()
    snapshot["operations"] = {
        name: operations[name]
        for name in ("stages", "amplification", "races", "completed_generation_wait_seconds_per_window_second")
    }
    snapshot["operations"]["load"] = load_snapshot()
    return snapshot


async def _build_dashboard_event() -> dict:
    raw, sources = await _read_dashboard_sources(("system", "metrics", "traffic", "queue"))
    payload: dict = {"timestamp": datetime.datetime.now(datetime.UTC).isoformat(), "sources": sources}
    if sources["system"]["available"]:
        payload["system"] = raw["system"]
    if sources["queue"]["available"]:
        queue = raw["queue"]
        payload["queue"] = {"pending": queue.get("pending_tasks"), "processing": queue.get("running_tasks")}
    if sources["metrics"]["available"]:
        metrics = raw["metrics"]
        payload["metrics"] = {
            "total_requests": metrics.get("total_requests"),
            "error_count": metrics.get("error_count"),
        }
    payload.setdefault("metrics", {})["traffic"] = _dashboard_source(raw, "traffic", None)[0]
    payload["lifecycle"] = bot_lifecycle.snapshot()
    return payload


@quart_app.route("/api/dashboard")
@require_auth
@rate_limit_api
async def api_dashboard():
    """Aggregated dashboard data — all operational metrics in a single response.

    Replaces 8 separate fetch() calls on the frontend with 1 HTTP round-trip.
    Each section is fetched in parallel; individual failures are isolated.
    """
    return jsonify(await _build_dashboard_snapshot())


# ── Key health endpoint ──────────────────────────────────────────────────────


@quart_app.route("/api/key-health")
@require_auth
@rate_limit_api
async def api_key_health():
    """Expose per-key health summary for dashboard diagnostics."""
    try:
        from app.repos.keys import KeyStatusManager

        manager = KeyStatusManager()
        async with database.db_manager.pool.acquire() as conn, conn.transaction():
            await database.set_user_context(settings.ADMIN_ID, True, conn=conn)
            try:
                summary = await manager.get_health_summary(conn=conn)
            finally:
                await database.clear_user_context(conn=conn)

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
                "keys": summary,
            }
        )
    except Exception as e:
        logging.warning("Key health endpoint error (%s)", type(e).__name__)
        return jsonify({"error": "source_unavailable"}), 500


# ── SSE live updates ─────────────────────────────────────────────────────────


@quart_app.route("/api/events")
@require_auth
async def api_events():
    """Server-Sent Events stream for real-time dashboard updates.

    Emits a JSON payload every 5 seconds with key operational metrics.
    Client connects via `new EventSource('/api/events')`.
    """

    async def generate():
        while True:
            try:
                payload = await _build_dashboard_event()
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                logging.warning("SSE event generation error (%s)", type(e).__name__)
                yield f"data: {json.dumps({'error': 'source_unavailable'})}\n\n"

            await asyncio.sleep(5)

    return generate(), {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }


# ── Unified Admin Daily & Broadcast Center ───────────────────────────────────


@quart_app.route("/admin_daily")
@require_auth
async def admin_daily_page():
    """Unified Daily Admin: Broadcast, Croc, 2048, Horoscope, Tarot."""
    from app.repos.crocodile_daily import today_puzzle_date
    from app.web_controls import _csrf

    return await render_template(
        "admin_daily.html",
        csrf_token=_csrf(),
        daily_bootstrap={"today": today_puzzle_date().isoformat(), "timezone": "Europe/Kyiv"},
    )


@quart_app.route("/admin_dailycroc")
@require_auth
async def admin_dailycroc_page():
    """Legacy redirect → /admin_daily#croc."""
    return redirect("/admin_daily#croc", code=301)


@quart_app.route("/admin_daily2048")
@require_auth
async def admin_daily2048_page():
    """Legacy redirect → /admin_daily#2048."""
    return redirect("/admin_daily#2048", code=301)


def _serialize_dailycroc_puzzle(puzzle) -> dict:
    return {
        "date": puzzle.puzzle_date.isoformat(),
        "difficulty": puzzle.difficulty,
        "target_word": puzzle.target_word,
        "topic": puzzle.topic,
        "image_file_id": puzzle.image_file_id,
        "image_prompt": puzzle.image_prompt,
        "image_model": puzzle.image_model,
        "content_revision": puzzle.content_revision,
        "played": puzzle.played,
        "gameplay_locked": puzzle.played,
    }


def _dailycroc_revision(data) -> int:
    value = data.get("expected_revision")
    if type(value) is not int or value < 1:
        raise ValueError("expected_revision required")
    return value


def _dailycroc_conflict(exc):
    return jsonify(
        {
            "error": exc.reason,
            "reason": exc.reason,
            "actual_revision": exc.actual_revision,
            "puzzle": _serialize_dailycroc_puzzle(exc.puzzle) if exc.puzzle else None,
        }
    ), 409


def _daily_settings_revision(data) -> int:
    value = data.get("expected_revision")
    if type(value) is not int or value < 0:
        raise ValueError("expected_revision required")
    return value


async def _daily_settings_snapshot():
    from app.runtime_settings.store import get_snapshot

    snapshot = await get_snapshot(force=True)
    if snapshot.degraded:
        raise ConnectionError("Runtime settings unavailable")
    return snapshot


async def _dailycroc_text_settings(snapshot):
    from app.config import is_gemini_chat_model_id
    from app.games.crocodile_daily import get_daily_image_model
    from app.games.daily_ai import TEXT_MODEL_PROCESSES, get_daily_text_model
    from app.process_policies import resolve_policy_value
    from app.repos.settings_repo import get_global_setting
    from app.runtime_settings.legacy_models import read_value
    from app.runtime_settings.lifecycle import runtime_settings_scope

    async with runtime_settings_scope(snapshot):
        shared = await get_daily_text_model()
        roles = []
        for process, label in TEXT_MODEL_PROCESSES.items():
            key = f"daily_croc_text_model_{process}"
            old = await get_global_setting(key, "")
            legacy = await read_value(key, old if is_gemini_chat_model_id(old) else "")
            baseline = legacy or shared
            policy = resolve_policy_value(
                f"crocodile.{process}",
                snapshot.values.get(f"process:crocodile.{process}"),
                (baseline,) if baseline else (),
                revision=snapshot.revision,
            )
            roles.append(
                {
                    "id": process,
                    "label": label,
                    "model": policy.models[0] if policy.explicit else legacy,
                    "effective_model": policy.models[0] if policy.models else "",
                    "effective_models": list(policy.models),
                    "source": "process" if policy.explicit else "role" if legacy else "shared" if shared else "auto",
                }
            )
        default_image_model = await get_daily_image_model()
    return {
        "settings_revision": snapshot.revision,
        "text_model": shared,
        "text_processes": roles,
        "default_image_model": default_image_model,
        "image_source": "runtime" if "legacy_model:daily_croc_image_model" in snapshot.values else "legacy",
        "source": "runtime" if "legacy_model:daily_croc_text_model" in snapshot.values else "legacy",
    }


async def _dailytrivia_model_settings(snapshot):
    from app.process_policies import baseline_models, resolve_policy_value
    from app.runtime_settings.lifecycle import runtime_settings_scope

    async with runtime_settings_scope(snapshot):
        value = snapshot.values.get("process:daily.trivia")
        policy = resolve_policy_value(
            "daily.trivia",
            value,
            await baseline_models("daily.trivia") if value is None else (),
            revision=snapshot.revision,
        )
    return {
        "settings_revision": snapshot.revision,
        "llm_model": policy.models[0],
        "effective_models": list(policy.models),
        "source": "process" if policy.explicit else "baseline",
    }


async def _daily_settings_conflict(kind):
    try:
        snapshot = await _daily_settings_snapshot()
        state = await (_dailycroc_text_settings(snapshot) if kind == "croc" else _dailytrivia_model_settings(snapshot))
    except Exception:
        return jsonify(error="settings unavailable; reload before retrying"), 503
    return jsonify(
        error="settings changed; reload before saving",
        reason="revision_conflict",
        settings_revision=snapshot.revision,
        settings=state,
    ), 409


async def _dailycroc_validated_image_model(model):
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("invalid image model")
    if model in {"fta-gpt-image-2", "vhr/gpt_image_2"}:
        return "fta-gpt-image-2"
    from app.providers.pollinations import fetch_models

    try:
        catalog = await fetch_models("image")
    except Exception as exc:
        raise ConnectionError("image model catalog unavailable") from exc
    match = next((item for item in catalog if model in [item["id"], *item.get("aliases", [])]), None)
    if match is None:
        raise ValueError("unknown image model")
    return match["id"]


@quart_app.route("/api/admin/dailycroc", methods=["GET"])
@require_auth
async def api_admin_dailycroc_list():
    from app import database as db

    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    from app.repos import crocodile_daily as repo

    rows = await db.db_query(
        """SELECT p.*, EXISTS(SELECT 1 FROM public.crocodile_daily_results r
               WHERE r.puzzle_date=p.puzzle_date AND r.difficulty=p.difficulty) AS played
           FROM public.crocodile_daily_puzzles p
           ORDER BY puzzle_date DESC,difficulty ASC LIMIT $1""",
        (limit,),
    )
    return jsonify(
        {"puzzles": [_serialize_dailycroc_puzzle(repo._row_to_puzzle(dict(row))) for row in rows if row["puzzle_date"]]}
    )


@quart_app.route("/api/admin/dailycroc/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailycroc_regen():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("croc.regenerate_image")


@quart_app.route("/api/admin/dailycroc/prompt", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_prompt():

    data = await request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    prompt = data.get("prompt")
    if not puzzle_date or not difficulty or not isinstance(prompt, str):
        return jsonify({"error": "missing fields"}), 400
    import datetime

    from app.repos import crocodile_daily as repo

    try:
        expected_revision = _dailycroc_revision(data)
        dt = datetime.date.fromisoformat(puzzle_date)
    except TypeError, ValueError:
        return jsonify({"error": "invalid date or expected_revision"}), 400

    try:
        image_model = data.get("image_model")
        if image_model is not None:
            image_model = await _dailycroc_validated_image_model(image_model)
        puzzle = await repo.edit_puzzle(dt, difficulty, expected_revision, image_prompt=prompt, image_model=image_model)
    except repo.DailyCrocConflict as exc:
        return _dailycroc_conflict(exc)
    except ValueError:
        return jsonify(error="invalid image model"), 400
    except ConnectionError:
        return jsonify(error="image model catalog unavailable; unchanged"), 503
    return jsonify({"success": True, "puzzle": _serialize_dailycroc_puzzle(puzzle)})


@quart_app.route("/api/admin/dailycroc/model", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_model():
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    model = data.get("model")
    if not puzzle_date or not difficulty or not isinstance(model, str) or not model.strip() or len(model) > 200:
        return jsonify({"error": "missing fields"}), 400
    import datetime

    from app.repos import crocodile_daily as repo

    try:
        expected_revision = _dailycroc_revision(data)
        dt = datetime.date.fromisoformat(puzzle_date)
    except TypeError, ValueError:
        return jsonify({"error": "invalid date or expected_revision"}), 400

    try:
        model = await _dailycroc_validated_image_model(model)
        puzzle = await repo.edit_puzzle(dt, difficulty, expected_revision, image_model=model)
    except repo.DailyCrocConflict as exc:
        return _dailycroc_conflict(exc)
    except ValueError:
        return jsonify(error="invalid image model"), 400
    except ConnectionError:
        return jsonify(error="image model catalog unavailable; unchanged"), 503
    return jsonify({"success": True, "puzzle": _serialize_dailycroc_puzzle(puzzle)})


@quart_app.route("/api/admin/dailycroc/models", methods=["GET"])
@require_auth
async def api_admin_dailycroc_models():
    from app.config import is_gemini_chat_model_id
    from app.providers.pollinations import fetch_models

    try:
        state = await _dailycroc_text_settings(await _daily_settings_snapshot())
    except Exception:
        return jsonify(error="model settings unavailable"), 503

    try:
        image_models = [{**model, "source": "pollinations"} for model in await fetch_models("image")]
        catalog_unavailable = False
    except Exception:
        image_models = []
        catalog_unavailable = True
    text_models = [{"id": "", "title": "Авто (текущая схема)", "aliases": []}]
    text_models.extend(
        {"id": model, "title": model, "aliases": []}
        for model in settings.AVAILABLE_MODELS
        if is_gemini_chat_model_id(model)
    )
    image_models.append(
        {"id": "fta-gpt-image-2", "title": "GPT Image 2 (FTA)", "aliases": ["vhr/gpt_image_2"], "source": "fta"}
    )
    return jsonify(
        {
            "image_models": image_models,
            "text_models": text_models,
            **state,
            "catalog_unavailable": catalog_unavailable,
        }
    )


@quart_app.route("/api/admin/dailycroc/text-model", methods=["POST"])
@require_auth
async def api_admin_dailycroc_text_model():
    from app.config import is_gemini_chat_model_id
    from app.games.daily_ai import TEXT_MODEL_PROCESSES
    from app.runtime_settings.legacy_models import save_croc_model_at_revision
    from app.runtime_settings.store import RevisionConflict

    data = await request.get_json()
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or len(model) > 200:
        return jsonify({"error": "invalid model"}), 400
    if model and (model not in settings.AVAILABLE_MODELS or not is_gemini_chat_model_id(model)):
        return jsonify({"error": "unknown Gemini text model"}), 400
    process = data.get("process", "")
    if not isinstance(process, str) or (process and process not in TEXT_MODEL_PROCESSES):
        return jsonify({"error": "unknown text process"}), 400
    try:
        expected_revision = _daily_settings_revision(data)
    except ValueError:
        return jsonify(error="expected_revision required"), 400
    try:
        saved = await save_croc_model_at_revision(process, model, expected_revision=expected_revision, actor="admin")
    except RevisionConflict:
        return await _daily_settings_conflict("croc")
    except ValueError:
        return jsonify({"error": "invalid model policy"}), 400
    except Exception:
        return jsonify({"error": "model setting was not confirmed; reload before retrying"}), 503
    return jsonify(
        {
            "success": True,
            "model": model,
            "process": process,
            "settings_revision": saved.revision,
            "settings": await _dailycroc_text_settings(saved),
        }
    )


@quart_app.route("/api/admin/dailycroc/image-default", methods=["POST"])
@require_auth
async def api_admin_dailycroc_image_default():
    from app.runtime_settings.legacy_models import save_croc_image_model_at_revision
    from app.runtime_settings.store import RevisionConflict

    data = await request.get_json()
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        return jsonify({"error": "invalid model"}), 400
    try:
        expected_revision = _daily_settings_revision(data)
        model = await _dailycroc_validated_image_model(model)
        saved = await save_croc_image_model_at_revision(model, expected_revision=expected_revision, actor="admin")
    except RevisionConflict:
        return await _daily_settings_conflict("croc")
    except ValueError:
        return jsonify(error="invalid model or expected_revision"), 400
    except ConnectionError:
        return jsonify(error="image model state unavailable; unchanged"), 503
    return jsonify(
        success=True, model=model, settings_revision=saved.revision, settings=await _dailycroc_text_settings(saved)
    )


@quart_app.route("/api/admin/dailycroc/image-quota", methods=["GET", "POST"])
@require_auth
async def api_admin_dailycroc_image_quota():
    from app.games.crocodile_daily import (
        DAILY_IMAGE_QUOTA_MAX,
        DAILY_IMAGE_QUOTA_SETTING_KEY,
        get_daily_image_quota_limit,
    )
    from app.repos.settings_repo import set_global_setting

    if request.method == "GET":
        return jsonify({"limit": await get_daily_image_quota_limit()})
    data = await request.get_json()
    limit = data.get("limit") if isinstance(data, dict) else None
    if type(limit) is not int or not 0 <= limit <= DAILY_IMAGE_QUOTA_MAX:
        return jsonify({"error": f"limit must be an integer from 0 to {DAILY_IMAGE_QUOTA_MAX}"}), 400
    await set_global_setting(DAILY_IMAGE_QUOTA_SETTING_KEY, str(limit))
    return jsonify({"success": True, "limit": limit})


@quart_app.route("/api/admin/dailycroc/day", methods=["GET"])
@require_auth
async def api_admin_dailycroc_day():
    from datetime import date

    value = request.args.get("date", "")
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError("non-canonical date")
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    from app.games.daily_preparation import get_day_readiness

    return jsonify(await get_day_readiness(day))


@quart_app.route("/api/admin/dailycroc/day/prepare", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailycroc_prepare_day():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("croc.prepare_day")


@quart_app.route("/api/admin/dailycroc/reset-word", methods=["POST"])
@require_auth
async def api_admin_dailycroc_reset_word():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("croc.reset_word")


@quart_app.route("/api/admin/dailycroc/image", methods=["GET"])
@require_auth
async def api_admin_dailycroc_image():
    """Proxy a Telegram file_id as raw image bytes for dashboard preview."""
    file_id = request.args.get("file_id", "")
    if not file_id:
        return jsonify({"error": "missing file_id"}), 400

    try:
        import io

        from quart import Response

        from app.bot_instance import get_bot
        from app.utils.tg_file import get_file_bytes

        bot = get_bot()
        if bot is None:
            return jsonify({"error": "bot_not_ready"}), 503

        tg_file = await bot.get_file(file_id)
        data = await get_file_bytes(bot, tg_file)
        return Response(
            io.BytesIO(data).read(),
            status=200,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": "public, max-age=3600",
            },
        )
    except Exception as exc:
        logging.error("Admin image proxy failed file_id=%s: %s", file_id, exc, exc_info=True)
        return jsonify({"error": "proxy_error", "detail": str(exc)}), 502


@quart_app.route("/api/admin/game-cover/<game_id>", methods=["GET"])
@require_auth
async def api_admin_get_game_cover(game_id: str):
    """Serve cover photo for a daily game (from disk or cached Telegram file_id)."""
    from quart import Response, send_file

    from app.games import cover_photo
    from app.repos.settings_repo import get_global_setting

    setting_key, file_path = cover_photo.get_game_keys(game_id)
    if file_path.exists():
        return await send_file(file_path, mimetype="image/png")

    file_id = await get_global_setting(setting_key, "")
    if file_id:
        try:
            import io

            from app.bot_instance import get_bot
            from app.utils.tg_file import get_file_bytes

            bot = get_bot()
            if bot is not None:
                tg_file = await bot.get_file(file_id)
                data = await get_file_bytes(bot, tg_file)
                return Response(
                    io.BytesIO(data).read(),
                    status=200,
                    headers={
                        "Content-Type": "image/jpeg",
                        "Cache-Control": "no-cache",
                    },
                )
        except Exception as exc:
            logging.warning("Failed to fetch game cover from Telegram: %s", exc)

    return jsonify({"error": "no_cover"}), 404


@quart_app.route("/api/admin/game-cover/<game_id>", methods=["POST"])
@require_auth
async def api_admin_upload_game_cover(game_id: str):
    """Upload a new cover photo file for a daily game."""
    files = await request.files
    uploaded_file = files.get("file")
    if not uploaded_file:
        return jsonify({"error": "missing file"}), 400

    image_bytes = uploaded_file.read()
    if not image_bytes:
        return jsonify({"error": "empty file"}), 400

    from app.games import cover_photo

    file_path = await cover_photo.set_cover_from_upload(game_id, image_bytes)
    return jsonify({"success": True, "path": file_path.name})


@quart_app.route("/api/admin/dailycroc/stats", methods=["GET"])
@require_auth
async def api_admin_dailycroc_stats():
    """Return real-time daily crocodile operational stats."""
    from app.repos import crocodile_daily as repo
    from app.repos.settings_repo import get_global_setting

    try:
        now = datetime.datetime.now(datetime.UTC)
        today = repo.today_puzzle_date(now)
        stats = await repo.get_delivery_status(today)

        # Get delivery enabled status
        delivery_on = await get_global_setting(repo.DAILY_DELIVERY_SETTING_KEY, "on")
        stats["delivery_enabled"] = delivery_on.strip().lower() != "off"

        # Get configured image model
        from app.games.crocodile_daily import get_daily_image_model

        image_model = await get_daily_image_model()
        stats["image_model"] = image_model.strip()

        # Get placeholder banner setting
        placeholder = await get_global_setting("daily_croc_placeholder_file_id", "")
        stats["placeholder_set"] = bool(placeholder)

        return jsonify(stats)
    except Exception as exc:
        logging.error("Failed to get daily croc stats: %s", exc, exc_info=True)
        return jsonify({"error": "failed_to_load_stats", "detail": str(exc)}), 500


@quart_app.route("/api/admin/dailycroc/toggle-delivery", methods=["POST"])
@require_auth
async def api_admin_dailycroc_toggle_delivery():
    """Enable or disable global daily crocodile subscription sends."""
    from app.repos import crocodile_daily as repo
    from app.repos.settings_repo import set_global_setting

    try:
        data = await request.get_json()
        if not data:
            return jsonify({"error": "invalid json"}), 400
        enabled = bool(data.get("enabled"))
        value = "on" if enabled else "off"
        await set_global_setting(repo.DAILY_DELIVERY_SETTING_KEY, value)
        return jsonify({"success": True, "enabled": enabled})
    except Exception as exc:
        logging.error("Failed to toggle daily croc delivery: %s", exc, exc_info=True)
        return jsonify({"error": "toggle_failed", "detail": str(exc)}), 500


# ── Admin Daily 2048 Sprint Dashboard ───────────────────────────────────


def _serialize_daily2048_puzzle(puzzle: daily_2048_repo.Daily2048Puzzle) -> dict:
    return {
        "date": puzzle.puzzle_date.isoformat(),
        "board": puzzle.board,
        "goal_type": puzzle.goal_type,
        "goal_value": puzzle.goal_value,
        "goal_label": daily_2048_game.goal_payload(puzzle)["label"],
        "spawn_sequence": puzzle.spawn_sequence,
        "seed": puzzle.seed,
        "par_moves": puzzle.par_moves,
        "target_seconds": puzzle.target_seconds,
        "status": puzzle.status,
        "solution_moves": puzzle.solution_moves,
        "prepared_at": puzzle.prepared_at.isoformat() if puzzle.prepared_at else "",
        "revision": puzzle.revision,
        "played": puzzle.played,
        "gameplay_locked": puzzle.played,
        "unavailable_reason": puzzle.status if puzzle.status != "ready" else None,
    }


@quart_app.route("/api/admin/daily2048", methods=["GET"])
@require_auth
async def api_admin_daily2048_list():
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    puzzles = await daily_2048_repo.list_puzzles(limit=max(1, min(limit, 90)))
    return jsonify({"puzzles": [_serialize_daily2048_puzzle(puzzle) for puzzle in puzzles]})


@quart_app.route("/api/admin/daily2048/puzzle", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_daily2048_save_puzzle():
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    if "expected_revision" not in data:
        return jsonify({"error": "expected_revision required"}), 400
    try:
        puzzle_date = datetime.date.fromisoformat(str(data.get("date") or ""))
        expected_revision = data["expected_revision"]
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid revision")
        board = daily_2048_repo.normalize_board(data.get("board"))
        goal_type = daily_2048_repo.normalize_goal_type(str(data.get("goal_type") or "tile"))
        goal_value = int(data.get("goal_value") or 512)
        spawn_sequence = daily_2048_repo.normalize_spawn_sequence(data.get("spawn_sequence"))
        seed = str(data.get("seed") or f"custom:{puzzle_date.isoformat()}").strip()
        par_moves = int(data.get("par_moves") or daily_2048_repo.DEFAULT_PAR_MOVES)
        target_seconds = int(data.get("target_seconds") or daily_2048_repo.DEFAULT_TARGET_SECONDS)
        status = str(data.get("status") or "draft").strip().lower()
        solution_moves = str(data.get("solution_moves") or "")
    except TypeError, ValueError:
        return jsonify({"error": "invalid puzzle payload"}), 400

    if status not in {"draft", "ready", "disabled"}:
        return jsonify({"error": "invalid status"}), 400
    if goal_value < 8:
        return jsonify({"error": "invalid goal_value"}), 400

    try:
        puzzle = await daily_2048_repo.upsert_puzzle(
            puzzle_date,
            board=board,
            goal_type=goal_type,
            goal_value=goal_value,
            spawn_sequence=spawn_sequence,
            seed=seed,
            par_moves=par_moves,
            target_seconds=target_seconds,
            status=status,
            solution_moves=solution_moves,
            expected_revision=expected_revision,
        )
    except daily_2048_repo.DailyPuzzleConflict as exc:
        return jsonify(
            {
                "error": exc.reason,
                "reason": exc.reason,
                "actual_revision": exc.actual_revision,
                "puzzle": _serialize_daily2048_puzzle(exc.puzzle) if exc.puzzle else None,
            }
        ), 409
    return jsonify({"success": True, "puzzle": _serialize_daily2048_puzzle(puzzle)})


@quart_app.route("/api/admin/daily2048/status", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_daily2048_status():
    data = await request.get_json() or {}
    if "expected_revision" not in data:
        return jsonify({"error": "expected_revision required"}), 400
    try:
        puzzle_date = datetime.date.fromisoformat(str(data.get("date") or ""))
        puzzle = await daily_2048_repo.set_puzzle_status(
            puzzle_date, status=data.get("status"), expected_revision=data["expected_revision"]
        )
    except TypeError, ValueError:
        return jsonify({"error": "invalid status payload"}), 400
    except daily_2048_repo.DailyPuzzleConflict as exc:
        return jsonify(
            {
                "error": exc.reason,
                "reason": exc.reason,
                "actual_revision": exc.actual_revision,
                "puzzle": _serialize_daily2048_puzzle(exc.puzzle) if exc.puzzle else None,
            }
        ), 409
    return jsonify({"success": True, "puzzle": _serialize_daily2048_puzzle(puzzle)})


@quart_app.route("/api/admin/daily2048/generate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_daily2048_generate_default():
    data = await request.get_json() or {}
    try:
        puzzle_date = datetime.date.fromisoformat(str(data.get("date") or daily_2048_repo.today_puzzle_date()))
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    puzzle = await daily_2048_repo.ensure_puzzle(puzzle_date)
    return jsonify({"success": True, "puzzle": _serialize_daily2048_puzzle(puzzle)})


@quart_app.route("/api/admin/daily-mode", methods=["POST"])
@require_auth
async def api_admin_daily_mode():
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    mode = str(data.get("mode") or "").strip().lower()
    if mode not in {
        daily_2048_repo.DAILY_GAME_MODE_CROCODILE,
        daily_2048_repo.DAILY_GAME_MODE_2048,
        daily_2048_repo.DAILY_GAME_MODE_TRIVIA,
    }:
        return jsonify({"error": "invalid mode"}), 400
    await set_global_setting(daily_2048_repo.DAILY_GAME_MODE_SETTING_KEY, mode)
    return jsonify({"success": True, "mode": mode})


def _parse_admin_trivia_question(raw, index: int):
    from app.games.daily_trivia_authoring import parse_admin_question

    return parse_admin_question(raw, index)


def _serialize_trivia_conflict(exc):
    conflict = exc.conflict
    return {
        "message": str(exc),
        "method": conflict.match.method,
        "confidence": conflict.match.score,
        "reason": conflict.match.reason,
        "existing": {
            "date": conflict.existing.puzzle_date.isoformat() if conflict.existing.puzzle_date else None,
            "lane": conflict.existing.lane,
            "position": conflict.existing.position,
            "question": conflict.existing.question,
            "identity": {
                "subject": conflict.existing.identity.subject,
                "relation": conflict.existing.identity.relation,
                "answer": conflict.existing.identity.answer,
            },
        },
    }


def _serialize_daily_trivia_puzzle(puzzle):
    if not puzzle:
        return None
    main_count = len(puzzle.questions)
    super_count = len(puzzle.super_questions)
    return {
        "date": puzzle.puzzle_date.isoformat(),
        "status": puzzle.status,
        "prepared_at": puzzle.prepared_at.isoformat() if puzzle.prepared_at else None,
        "revision": puzzle.revision,
        "published_revision_id": puzzle.published_revision_id,
        "readiness": {
            "main": {"count": main_count, "required": 5, "ready": main_count == 5},
            "super": {"count": super_count, "required": 3, "ready": super_count == 3},
            "publishable": main_count == 5 and super_count == 3,
        },
        "questions": [
            {
                "id": q.id,
                "topic": q.topic,
                "question": q.question,
                "options": q.options,
                "correct_index": q.correct_index,
                "explanation": q.explanation,
                "identity": (
                    {
                        "subject": q.identity.subject,
                        "relation": q.identity.relation,
                        "answer": q.identity.answer,
                        "identity_hash": q.identity.identity_hash,
                    }
                    if q.identity
                    else None
                ),
            }
            for q in puzzle.questions
        ],
        "super_questions": [
            {
                "id": q.id,
                "topic": q.topic,
                "question": q.question,
                "options": q.options,
                "correct_index": q.correct_index,
                "explanation": q.explanation,
                "identity": (
                    {
                        "subject": q.identity.subject,
                        "relation": q.identity.relation,
                        "answer": q.identity.answer,
                        "identity_hash": q.identity.identity_hash,
                    }
                    if q.identity
                    else None
                ),
            }
            for q in puzzle.super_questions
        ],
    }


@quart_app.route("/api/admin/dailytrivia/stats", methods=["GET"])
@require_auth
async def api_admin_dailytrivia_stats():
    from app.config import GEMINI_ROLE_MODEL_ALIASES
    from app.repos import daily_trivia as daily_trivia_repo
    from app.repos.crocodile_daily import today_puzzle_date
    from app.repos.settings_repo import get_global_setting

    today = today_puzzle_date()
    stats = await daily_trivia_repo.get_admin_stats(today)
    delivery_on = await get_global_setting("daily_trivia_delivery_enabled", "on")
    stats["delivery_enabled"] = delivery_on.strip().lower() != "off"
    try:
        stats.update(await _dailytrivia_model_settings(await _daily_settings_snapshot()))
    except Exception:
        return jsonify(error="model settings unavailable"), 503
    configured_models = getattr(settings, "AVAILABLE_MODELS", []) if settings is not None else []
    stats["llm_models"] = list(
        dict.fromkeys(
            [
                *GEMINI_ROLE_MODEL_ALIASES,
                *(model for model in configured_models if isinstance(model, str) and model.strip()),
            ]
        )
    )
    delivery_info = await daily_trivia_repo.get_delivery_status(today)
    stats["sent_today"] = delivery_info.get("sent_today", 0)
    stats["pending_today"] = delivery_info.get("pending_today", 0)
    return jsonify(stats)


@quart_app.route("/api/admin/dailytrivia/settings", methods=["POST"])
@require_auth
async def api_admin_dailytrivia_settings():
    from app.config import GEMINI_ROLE_MODEL_ALIASES, is_gemini_chat_model_id
    from app.providers.gemini import GeminiModelValidationStatus, validate_gemini_chat_model_capability
    from app.repos.settings_repo import set_global_setting

    data = await request.get_json() or {}
    if not isinstance(data, dict):
        return jsonify(error="invalid settings"), 400
    saved_model: str | None = None
    saved_settings = None
    if "llm_model" in data:
        try:
            expected_revision = _daily_settings_revision(data)
        except ValueError:
            return jsonify(error="expected_revision required"), 400
        model = str(data["llm_model"]).strip()
        if not model or (model not in GEMINI_ROLE_MODEL_ALIASES and not is_gemini_chat_model_id(model)):
            return jsonify({"success": False, "error": "Некорректный Gemini model ID"}), 400
        if model not in GEMINI_ROLE_MODEL_ALIASES:
            validation = await validate_gemini_chat_model_capability(model)
            if validation is GeminiModelValidationStatus.UNSUPPORTED:
                return jsonify(
                    {"success": False, "error": "Модель не существует или не поддерживает generateContent"}
                ), 400
            if validation is not GeminiModelValidationStatus.SUPPORTED:
                return jsonify({"success": False, "error": "Не удалось проверить модель Gemini. Повторите позже"}), 503
        from app.config import normalize_gemini_runtime_model
        from app.runtime_settings.processes import save_primary_model_at_revision
        from app.runtime_settings.store import RevisionConflict

        model = normalize_gemini_runtime_model(model, fallback="")
        try:
            saved_settings = await save_primary_model_at_revision(
                "daily.trivia", model, expected_revision=expected_revision, actor="admin"
            )
        except RevisionConflict:
            return await _daily_settings_conflict("trivia")
        except Exception:
            return jsonify(success=False, error="Не удалось подтвердить сохранение модели."), 503
        saved_model = model
    if "delivery_enabled" in data:
        val = "on" if bool(data["delivery_enabled"]) else "off"
        await set_global_setting("daily_trivia_delivery_enabled", val)
    response = {"success": True}
    if saved_model is not None:
        response["llm_model"] = saved_model
        response["settings_revision"] = saved_settings.revision
        response["settings"] = await _dailytrivia_model_settings(saved_settings)
    return jsonify(response)


@quart_app.route("/api/admin/dailytrivia/leaderboard", methods=["GET"])
@require_auth
async def api_admin_dailytrivia_leaderboard():
    from app.repos import daily_trivia as daily_trivia_repo
    from app.repos.crocodile_daily import today_puzzle_date

    lb_type = request.args.get("type", "daily")
    today = today_puzzle_date()

    if lb_type == "monthly":
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        items = await daily_trivia_repo.get_monthly_leaderboard(year, month)
    else:
        date_str = request.args.get("date")
        p_date = datetime.date.fromisoformat(date_str) if date_str else today
        items = await daily_trivia_repo.get_daily_leaderboard(p_date)

    return jsonify({"type": lb_type, "items": items})


@quart_app.route("/api/admin/dailytrivia", methods=["GET"])
@require_auth
async def api_admin_dailytrivia_list():
    from app.repos import daily_trivia as daily_trivia_repo
    from app.repos.crocodile_daily import today_puzzle_date

    today = today_puzzle_date()
    start_date = today - datetime.timedelta(days=14)
    end_date = today + datetime.timedelta(days=7)
    db_puzzles = await daily_trivia_repo.get_puzzles_range(start_date, end_date)

    db_puzzles_by_date = {p.puzzle_date: p for p in db_puzzles}
    all_puzzles = []

    curr = start_date
    while curr <= end_date:
        if curr in db_puzzles_by_date:
            all_puzzles.append(_serialize_daily_trivia_puzzle(db_puzzles_by_date[curr]))
        else:
            all_puzzles.append(
                {
                    "date": curr.isoformat(),
                    "status": "missing",
                    "prepared_at": None,
                    "revision": 0,
                    "published_revision_id": None,
                    "questions": [],
                    "super_questions": [],
                    "readiness": {
                        "main": {"count": 0, "required": 5, "ready": False},
                        "super": {"count": 0, "required": 3, "ready": False},
                        "publishable": False,
                    },
                }
            )
        curr += datetime.timedelta(days=1)

    all_puzzles.sort(key=lambda x: x["date"], reverse=True)

    return jsonify(
        {
            "puzzles": all_puzzles,
            "today": today.isoformat(),
        }
    )


@quart_app.route("/api/admin/dailytrivia/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_regenerate():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("trivia.regenerate")


@quart_app.route("/api/admin/dailytrivia/save", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_save():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("trivia.validate_save", part="main")


@quart_app.route("/api/admin/dailytrivia/save_super", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_save_super():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("trivia.validate_save", part="super")


@quart_app.route("/api/admin/dailytrivia/clear_used_keys", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_clear_used_keys():
    from app.repos import daily_trivia as daily_trivia_repo
    from app.repos.crocodile_daily import today_puzzle_date

    data = await request.get_json() or {}
    try:
        p_date = datetime.date.fromisoformat(str(data.get("date") or today_puzzle_date()))
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    deleted = await daily_trivia_repo.delete_used_keys_for_date(p_date)
    return jsonify({"success": True, "deleted": deleted})


# ── Broadcast Center API ──────────────────────────────────────────────────────


@quart_app.route("/api/admin/broadcast/overview", methods=["GET"])
@require_auth
async def api_admin_broadcast_overview():
    """Aggregate delivery stats across all channels for the Broadcast tab."""
    from app.repos import crocodile_daily as croc_repo
    from app.repos.settings_repo import get_global_setting

    try:
        now = datetime.datetime.now(datetime.UTC)
        today = croc_repo.today_puzzle_date(now)

        # --- Daily Challenge (Croc + 2048 unified subscription) ---
        delivery_on_raw = await get_global_setting(croc_repo.DAILY_DELIVERY_SETTING_KEY, "on")
        croc_enabled = delivery_on_raw.strip().lower() != "off"

        game_mode = await daily_2048_repo.get_active_daily_game_mode()
        if game_mode == daily_2048_repo.DAILY_GAME_MODE_TRIVIA:
            mode_emoji = "🧠"
            mode_label = "Daily Trivia"
        elif game_mode == daily_2048_repo.DAILY_GAME_MODE_2048:
            mode_emoji = "🎲"
            mode_label = "Daily 2048"
        else:
            mode_emoji = "🐊"
            mode_label = "Daily Croc"

        # Count total + pending for daily challenge
        total_subs_rows = await database.db_query(
            "SELECT COUNT(*) AS cnt FROM public.crocodile_daily_preferences WHERE is_subscribed = TRUE"
        )
        total_challenge_subs = int(total_subs_rows[0]["cnt"] if total_subs_rows else 0)

        pending_rows = await database.db_query(
            """
            SELECT COUNT(*) AS cnt
            FROM public.crocodile_daily_preferences
            WHERE is_subscribed = TRUE
              AND (last_sent_puzzle_date IS NULL OR last_sent_puzzle_date < $1)
            """,
            (today,),
        )
        pending_challenge = int(pending_rows[0]["cnt"] if pending_rows else 0)

        sent_challenge = total_challenge_subs - pending_challenge

        challenge_channel = {
            "id": "daily_challenge",
            "name": f"{mode_label} ({mode_emoji} {game_mode})",
            "emoji": "🎮",
            "active_game": game_mode,
            "subscribers": total_challenge_subs,
            "pending_today": pending_challenge,
            "sent_today": max(0, sent_challenge),
            "delivery_enabled": croc_enabled,
            "last_sent_at": None,
        }

        # --- Horoscope ---
        horo_enabled_raw = await get_global_setting("horoscope_delivery_enabled", "on")
        horo_enabled = horo_enabled_raw.strip().lower() != "off"

        horo_rows = await database.db_query(
            """
            SELECT
                COUNT(*) FILTER (WHERE is_active = TRUE) AS total_active,
                (
                    COUNT(*) FILTER (
                        WHERE is_active = TRUE
                          AND time_today IS NOT NULL
                          AND (last_today_sent IS NULL OR last_today_sent::date < CURRENT_DATE)
                    )
                    +
                    COUNT(*) FILTER (
                        WHERE is_active = TRUE
                          AND time_tomorrow IS NOT NULL
                          AND (last_tomorrow_sent IS NULL OR last_tomorrow_sent::date < CURRENT_DATE)
                    )
                ) AS pending_deliveries,
                (
                    COUNT(*) FILTER (
                        WHERE is_active = TRUE
                          AND last_today_sent::date = CURRENT_DATE
                    )
                    +
                    COUNT(*) FILTER (
                        WHERE is_active = TRUE
                          AND last_tomorrow_sent::date = CURRENT_DATE
                    )
                ) AS sent_deliveries_today
            FROM horoscope_subscriptions
            """
        )
        horo_stats = dict(horo_rows[0]) if horo_rows else {}
        horo_total = int(horo_stats.get("total_active", 0) or 0)
        horo_pending = int(horo_stats.get("pending_deliveries", 0) or 0)
        horo_sent_today = int(horo_stats.get("sent_deliveries_today", 0) or 0)

        horo_channel = {
            "id": "horoscope",
            "name": "Гороскоп",
            "emoji": "⭐",
            "active_game": None,
            "subscribers": horo_total,
            "pending_today": horo_pending,
            "sent_today": horo_sent_today,
            "delivery_enabled": horo_enabled,
            "last_sent_at": None,
        }

        # --- Tarot ---
        tarot_enabled_raw = await get_global_setting("tarot_daily_delivery_enabled", "off")
        tarot_enabled = tarot_enabled_raw.strip().lower() != "off"

        tarot_total_rows = await database.db_query(
            """
            SELECT COUNT(*) AS cnt FROM public.tarot_daily_subscriptions WHERE is_subscribed = TRUE
            """
        )
        tarot_total = int(tarot_total_rows[0]["cnt"] if tarot_total_rows else 0) if tarot_total_rows else 0

        from app.broadcast_jobs import observed_channel_counts
        from app.repos.crocodile_daily import today_puzzle_date

        try:
            async with asyncio.timeout(5):
                tarot_observed = await observed_channel_counts("tarot", date=today_puzzle_date().isoformat())
            tarot_observed["available"] = True
        except Exception:
            tarot_observed = {"available": False, "coverage_started_at": None}

        tarot_channel = {
            "id": "tarot",
            "name": "Карта дня (Таро)",
            "emoji": "🔮",
            "active_game": None,
            "subscribers": tarot_total,
            "pending_today": None,
            "sent_today": tarot_observed.get("sent"),
            "delivery_enabled": tarot_enabled,
            "last_sent_at": None,
            "delivery_journal": tarot_observed,
        }

        return jsonify({"channels": [challenge_channel, horo_channel, tarot_channel]})
    except Exception as exc:
        logging.error("Broadcast overview error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/broadcast/subscribers", methods=["GET"])
@require_auth
async def api_admin_broadcast_subscribers():
    """Unified subscriber list across all channels with filters."""
    from app.repos import crocodile_daily as croc_repo

    channel = request.args.get("channel", "")  # croc, horoscope, all
    status_filter = request.args.get("status", "")  # active, snoozed, error
    tz_filter = request.args.get("timezone", "")
    user_id_filter = request.args.get("user_id", "")
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        limit, offset = 50, 0

    try:
        now = datetime.datetime.now(datetime.UTC)
        today = croc_repo.today_puzzle_date(now)
        rows_out: list[dict] = []

        # --- Daily Challenge subscribers ---
        if not channel or channel in ("croc", "daily_challenge", "all"):
            where_clauses = ["p.is_subscribed = TRUE"]
            params: list = []
            idx = 1

            if tz_filter:
                where_clauses.append(f"p.timezone = ${idx}")
                params.append(tz_filter)
                idx += 1
            if user_id_filter:
                try:
                    where_clauses.append(f"p.user_id = ${idx}")
                    params.append(int(user_id_filter))
                    idx += 1
                except ValueError:
                    pass
            if status_filter == "snoozed":
                where_clauses.append("p.discovery_snoozed_until > NOW()")
            elif status_filter == "active":
                where_clauses.append(f"(p.last_sent_puzzle_date IS NULL OR p.last_sent_puzzle_date < ${idx})")
                params.append(today)
                idx += 1

            where_sql = " AND ".join(where_clauses)
            params.extend([limit, offset])
            croc_rows = await database.db_query(
                f"""
                SELECT p.user_id,
                       p.timezone,
                       p.preferred_local_hour,
                       p.last_sent_puzzle_date AS last_sent,
                       p.discovery_snoozed_until
                FROM public.crocodile_daily_preferences p
                WHERE {where_sql}
                ORDER BY p.user_id
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                tuple(params),
            )
            for r in croc_rows:
                snoozed = r.get("discovery_snoozed_until")
                status = "snoozed" if snoozed and snoozed > now else "active"
                last_sent = r.get("last_sent")
                rows_out.append(
                    {
                        "user_id": r["user_id"],
                        "channels": ["🎮 Daily Challenge"],
                        "timezone": r.get("timezone") or "—",
                        "preferred_hour": r.get("preferred_local_hour"),
                        "last_sent": last_sent.isoformat() if last_sent else None,
                        "status": status,
                    }
                )

        # --- Horoscope subscribers ---
        if not channel or channel in ("horoscope", "all"):
            horo_rows = await database.db_query(
                """
                SELECT user_id, utc_offset, sign,
                       last_today_sent, last_tomorrow_sent
                FROM horoscope_subscriptions
                WHERE is_active = TRUE
                ORDER BY user_id
                LIMIT $1 OFFSET $2
                """,
                (limit, offset),
            )
            croc_user_ids = {r["user_id"] for r in rows_out}
            for r in horo_rows:
                uid = r["user_id"]
                badge = "⭐ Horoscope"
                last_sent_val = r.get("last_today_sent") or r.get("last_tomorrow_sent")
                if uid in croc_user_ids:
                    # Merge: update existing entry's channels list
                    for entry in rows_out:
                        if entry["user_id"] == uid:
                            entry["channels"].append(badge)
                            break
                else:
                    rows_out.append(
                        {
                            "user_id": uid,
                            "channels": [badge],
                            "timezone": f"UTC{r['utc_offset']:+d}" if r.get("utc_offset") is not None else "—",
                            "preferred_hour": None,
                            "last_sent": last_sent_val.isoformat() if last_sent_val else None,
                            "status": "active",
                        }
                    )

        return jsonify({"total": len(rows_out), "rows": rows_out})
    except Exception as exc:
        logging.error("Broadcast subscribers error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/broadcast/toggle", methods=["POST"])
@require_auth
async def api_admin_broadcast_toggle():
    """Enable/disable delivery for a specific broadcast channel."""
    from app.repos import crocodile_daily as croc_repo
    from app.repos.settings_repo import set_global_setting

    try:
        data = await request.get_json()
        if not data:
            return jsonify({"error": "invalid json"}), 400
        channel = str(data.get("channel") or "").strip()
        enabled = bool(data.get("enabled"))

        if channel == "daily_challenge":
            value = "on" if enabled else "off"
            await set_global_setting(croc_repo.DAILY_DELIVERY_SETTING_KEY, value)
        elif channel == "horoscope":
            await set_global_setting("horoscope_delivery_enabled", "on" if enabled else "off")
        elif channel == "tarot":
            await set_global_setting("tarot_daily_delivery_enabled", "on" if enabled else "off")
        else:
            return jsonify({"error": f"unknown channel: {channel}"}), 400

        return jsonify({"success": True, "channel": channel, "enabled": enabled})
    except Exception as exc:
        logging.error("Broadcast toggle error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/broadcast/errors", methods=["GET"])
@require_auth
async def api_admin_broadcast_errors():
    from app.web_broadcast_jobs import journal_response

    try:
        return await journal_response()
    except ValueError:
        return jsonify(error="invalid_journal_filter"), 400
    except Exception:
        return jsonify(error="journal_unavailable", available=False), 503


# ── Broadcast Offer History & Manual Send ─────────────────────────────────────


@quart_app.route("/api/admin/broadcast/offer-history", methods=["GET"])
@require_auth
async def api_admin_broadcast_offer_history():
    """Unified offer history across all broadcast channels.

    Returns all reachable users with their offer/subscription state per channel.
    Supports filtering by channel, subscription status, and "never offered" flag.

    Query params:
      channel     – "daily_challenge" | "horoscope" | "tarot" | "all" (default)
      status      – "subscribed" | "not_subscribed" | "snoozed" | "" (all)
      never_sent  – "1" → only users who never received any offer
      user_id     – exact user_id filter
      limit       – 1..200 (default 50)
      offset      – pagination offset (default 0)
    """

    channel = request.args.get("channel", "all").strip()
    status_filter = request.args.get("status", "").strip()
    never_sent = request.args.get("never_sent", "").strip() == "1"
    user_id_raw = request.args.get("user_id", "").strip()
    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        limit, offset = 50, 0

    try:
        now = datetime.datetime.now(datetime.UTC)
        rows_out: list[dict] = []

        # ── Daily Challenge (Croc / 2048) ────────────────────────────────────
        if channel in ("daily_challenge", "all"):
            where: list[str] = []
            params: list = []
            idx = 1

            if user_id_raw:
                try:
                    where.append(f"u.user_id = ${idx}")
                    params.append(int(user_id_raw))
                    idx += 1
                except ValueError:
                    pass

            if never_sent:
                where.append("pref.discovery_last_sent_at IS NULL")

            if status_filter == "subscribed":
                where.append("COALESCE(pref.is_subscribed, FALSE) = TRUE")
            elif status_filter == "not_subscribed":
                where.append("COALESCE(pref.is_subscribed, FALSE) = FALSE")
            elif status_filter == "snoozed":
                where.append("pref.discovery_snoozed_until > NOW()")

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            params.extend([limit, offset])

            croc_rows = await database.db_query(
                f"""
                SELECT u.user_id,
                       u.display_name,
                       COALESCE(pref.is_subscribed, FALSE)     AS is_subscribed,
                       pref.discovery_last_sent_at,
                       pref.discovery_snoozed_until,
                       pref.last_sent_puzzle_date
                FROM public.users u
                LEFT JOIN public.crocodile_daily_preferences pref ON pref.user_id = u.user_id
                {where_sql}
                ORDER BY u.user_id
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                tuple(params),
            )
            for r in croc_rows:
                snoozed = r.get("discovery_snoozed_until")
                rows_out.append(
                    {
                        "user_id": r["user_id"],
                        "display_name": r.get("display_name"),
                        "channel": "daily_challenge",
                        "channel_emoji": "🎮",
                        "is_subscribed": bool(r.get("is_subscribed")),
                        "offer_sent_at": r["discovery_last_sent_at"].isoformat()
                        if r.get("discovery_last_sent_at")
                        else None,
                        "snoozed_until": snoozed.isoformat() if snoozed else None,
                        "snoozed_active": bool(snoozed and snoozed > now),
                        "last_delivery": r["last_sent_puzzle_date"].isoformat()
                        if r.get("last_sent_puzzle_date")
                        else None,
                    }
                )

        # ── Horoscope ────────────────────────────────────────────────────────
        if channel in ("horoscope", "all"):
            where_h: list[str] = []
            params_h: list = []
            idx_h = 1

            if user_id_raw:
                try:
                    where_h.append(f"u.user_id = ${idx_h}")
                    params_h.append(int(user_id_raw))
                    idx_h += 1
                except ValueError:
                    pass

            if never_sent:
                where_h.append("hs.discovery_last_sent_at IS NULL")

            if status_filter == "subscribed":
                where_h.append("COALESCE(hs.is_active, FALSE) = TRUE")
            elif status_filter == "not_subscribed":
                where_h.append("COALESCE(hs.is_active, FALSE) = FALSE")

            where_h_sql = ("WHERE " + " AND ".join(where_h)) if where_h else ""
            params_h.extend([limit, offset])

            horo_rows = await database.db_query(
                f"""
                SELECT u.user_id,
                       u.display_name,
                       COALESCE(hs.is_active, FALSE)       AS is_subscribed,
                       hs.discovery_last_sent_at,
                       hs.sign,
                       hs.last_today_sent
                FROM public.users u
                LEFT JOIN horoscope_subscriptions hs ON hs.user_id = u.user_id
                {where_h_sql}
                ORDER BY u.user_id
                LIMIT ${idx_h} OFFSET ${idx_h + 1}
                """,
                tuple(params_h),
            )
            existing_ids = {r["user_id"] for r in rows_out}
            for r in horo_rows:
                uid = r["user_id"]
                entry = {
                    "user_id": uid,
                    "display_name": r.get("display_name"),
                    "channel": "horoscope",
                    "channel_emoji": "⭐",
                    "is_subscribed": bool(r.get("is_subscribed")),
                    "offer_sent_at": r["discovery_last_sent_at"].isoformat()
                    if r.get("discovery_last_sent_at")
                    else None,
                    "snoozed_until": None,
                    "snoozed_active": False,
                    "last_delivery": r["last_today_sent"].isoformat() if r.get("last_today_sent") else None,
                }
                if uid not in existing_ids:
                    rows_out.append(entry)

        # ── Tarot ────────────────────────────────────────────────────────────
        if channel in ("tarot", "all"):
            where_t: list[str] = []
            params_t: list = []
            idx_t = 1

            if user_id_raw:
                try:
                    where_t.append(f"u.user_id = ${idx_t}")
                    params_t.append(int(user_id_raw))
                    idx_t += 1
                except ValueError:
                    pass

            if never_sent:
                where_t.append("ts.discovery_last_sent_at IS NULL")

            if status_filter == "subscribed":
                where_t.append("COALESCE(ts.is_subscribed, FALSE) = TRUE")
            elif status_filter == "not_subscribed":
                where_t.append("COALESCE(ts.is_subscribed, FALSE) = FALSE")

            where_t_sql = ("WHERE " + " AND ".join(where_t)) if where_t else ""
            params_t.extend([limit, offset])

            tarot_rows = await database.db_query(
                f"""
                SELECT u.user_id,
                       u.display_name,
                       COALESCE(ts.is_subscribed, FALSE)    AS is_subscribed,
                       ts.discovery_last_sent_at,
                       ts.last_sent_date
                FROM public.users u
                LEFT JOIN public.tarot_daily_subscriptions ts ON ts.user_id = u.user_id
                {where_t_sql}
                ORDER BY u.user_id
                LIMIT ${idx_t} OFFSET ${idx_t + 1}
                """,
                tuple(params_t),
            )
            existing_ids_2 = {r["user_id"] for r in rows_out}
            for r in tarot_rows:
                uid = r["user_id"]
                entry = {
                    "user_id": uid,
                    "display_name": r.get("display_name"),
                    "channel": "tarot",
                    "channel_emoji": "🔮",
                    "is_subscribed": bool(r.get("is_subscribed")),
                    "offer_sent_at": r["discovery_last_sent_at"].isoformat()
                    if r.get("discovery_last_sent_at")
                    else None,
                    "snoozed_until": None,
                    "snoozed_active": False,
                    "last_delivery": r["last_sent_date"].isoformat() if r.get("last_sent_date") else None,
                }
                if uid not in existing_ids_2:
                    rows_out.append(entry)

        return jsonify({"total": len(rows_out), "rows": rows_out})
    except Exception as exc:
        logging.error("Broadcast offer-history error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/broadcast/send-offer", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_broadcast_send_offer():
    from app.web_broadcast_jobs import legacy_enqueue

    return await legacy_enqueue(single=True)


@quart_app.route("/api/admin/broadcast/send-offer-batch", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_broadcast_send_offer_batch():
    from app.web_broadcast_jobs import legacy_enqueue

    return await legacy_enqueue()


# ── Horoscope Admin API ───────────────────────────────────────────────────────


@quart_app.route("/api/admin/horoscope/stats", methods=["GET"])
@require_auth
async def api_admin_horoscope_stats():
    """Horoscope subscription statistics."""
    try:
        rows = await database.db_query(
            """
            SELECT
                COUNT(*) FILTER (WHERE is_active = TRUE) AS total_active,
                COUNT(*) FILTER (WHERE is_active = TRUE AND time_today IS NOT NULL AND time_tomorrow IS NOT NULL) AS both_slots,
                COUNT(*) FILTER (WHERE is_active = TRUE AND time_today IS NOT NULL AND time_tomorrow IS NULL) AS today_only,
                COUNT(*) FILTER (WHERE is_active = TRUE AND time_today IS NULL AND time_tomorrow IS NOT NULL) AS tomorrow_only,
                COUNT(*) FILTER (WHERE is_active = FALSE) AS total_inactive
            FROM horoscope_subscriptions
            """
        )
        stats = dict(rows[0]) if rows else {}

        # Per-sign breakdown
        sign_rows = await database.db_query(
            """
            SELECT sign, COUNT(*) AS cnt
            FROM horoscope_subscriptions
            WHERE is_active = TRUE
            GROUP BY sign
            ORDER BY cnt DESC
            """
        )
        by_sign = {r["sign"]: int(r["cnt"]) for r in sign_rows}

        return jsonify(
            {
                "total_active": int(stats.get("total_active", 0)),
                "breakdown": {
                    "today_only": int(stats.get("today_only", 0)),
                    "tomorrow_only": int(stats.get("tomorrow_only", 0)),
                    "both": int(stats.get("both_slots", 0)),
                },
                "total_inactive": int(stats.get("total_inactive", 0)),
                "by_sign": by_sign,
            }
        )
    except Exception as exc:
        logging.error("Horoscope stats error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


# ── Tarot Admin API ───────────────────────────────────────────────────────────


@quart_app.route("/api/admin/tarot/status", methods=["GET"])
@require_auth
async def api_admin_tarot_status():
    """Status of prepared daily tarot readings for today and tomorrow."""
    try:
        from app.tarot_daily import today_reading_date

        today = today_reading_date()
        tomorrow = today + datetime.timedelta(days=1)

        async def _reading_counts(target_date) -> dict:
            rows = await database.db_query(
                """
                SELECT card_name, orientation,
                       body_markdown IS NOT NULL AND body_markdown <> '' AS ready
                FROM public.tarot_daily_readings
                WHERE reading_date = $1
                """,
                (target_date,),
            )
            total = len(rows)
            ready = sum(1 for r in rows if r.get("ready"))
            return {
                "date": target_date.isoformat(),
                "ready_count": ready,
                "total": total,
                "cards": [
                    {
                        "label": f"{r['card_name']} ({r['orientation']})",
                        "ready": bool(r.get("ready")),
                    }
                    for r in rows
                ],
            }

        today_status, tomorrow_status = await asyncio.gather(
            _reading_counts(today),
            _reading_counts(tomorrow),
        )

        # Tarot subscriber count
        try:
            sub_rows = await database.db_query(
                "SELECT COUNT(*) AS cnt FROM public.tarot_daily_subscriptions WHERE is_subscribed = TRUE"
            )
            subscriber_count = int(sub_rows[0]["cnt"] if sub_rows else 0)
        except Exception:
            subscriber_count = 0

        return jsonify(
            {
                "today": today_status,
                "tomorrow": tomorrow_status,
                "subscribers": subscriber_count,
            }
        )
    except Exception as exc:
        logging.error("Tarot status error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/tarot/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_tarot_regenerate():
    from app.web_daily_jobs import legacy_enqueue

    return await legacy_enqueue("tarot.prepare")


@quart_app.route("/api/admin/tarot/models")
@require_auth
@rate_limit_api
async def api_admin_tarot_models():
    from app.process_policies import validate_policy
    from app.repos.models_repo import get_models
    from app.tarot import iter_daily_card_variants
    from app.tarot_daily import resolve_daily_model_policy

    try:
        models = []
        for model in await get_models("gemini"):
            try:
                validate_policy("tarot.daily", {"models": [model], "strategy": "sequential"})
                models.append(model)
            except ValueError:
                continue
        policy = await resolve_daily_model_policy()
        return jsonify(
            models=models, effective_models=list(policy.models), variants=len(list(iter_daily_card_variants()))
        )
    except Exception:
        return jsonify(error="tarot_models_unavailable"), 503

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
from app.games import daily_2048 as daily_2048_game
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


# Derive a secret key for sessions from ADMIN_SECRET
def _get_admin_secret():
    """Returns the configured admin secret."""
    return getattr(settings, "ADMIN_SECRET", None) or os.environ.get("ADMIN_SECRET")


# Placeholder — overridden at server startup via @before_serving
quart_app.secret_key = os.urandom(32)  # Secure fallback — overridden at startup


@quart_app.before_serving
async def _init_secret_key():
    """Compute session secret_key at startup when ADMIN_SECRET is definitely available."""
    admin_secret = _get_admin_secret() or ""
    quart_app.secret_key = hashlib.sha256(f"gemaibotv2-session-{admin_secret}".encode()).hexdigest()
    if not admin_secret:
        logging.warning("ADMIN_SECRET is empty — session secret_key is weak")


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
async def generate_csp_nonce():
    """Generate a per-request CSP nonce for inline scripts/styles."""
    g.csp_nonce = secrets.token_urlsafe(16)


@quart_app.after_request
async def add_security_headers(response):
    """Add security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    nonce = getattr(g, "csp_nonce", "")
    is_natal_report = request.path.startswith("/reports/natal/")
    is_health_check = request.path == "/health"
    is_webapp = request.path.startswith("/webapp")

    if is_natal_report:
        response.headers["Referrer-Policy"] = "no-referrer"
        csp = (
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


# =============================================================================
# AUTHENTICATION
# =============================================================================

# _get_admin_secret() is defined above (before session key derivation).


def _is_authenticated():
    """Check if current request has a valid session, header token, or Telegram admin initData."""
    # Check session cookie first
    if session.get("authenticated"):
        return True
    # Fallback: check X-Auth-Token header (for API/monitoring tools)
    token = request.headers.get("X-Auth-Token")
    expected = _get_admin_secret()
    if token and expected and hmac.compare_digest(token, expected):
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
        import psutil

        system = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "memory_used_mb": round(psutil.virtual_memory().used / (1024 * 1024), 1),
            "memory_total_mb": round(psutil.virtual_memory().total / (1024 * 1024), 1),
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


@quart_app.route("/api/keys")
@require_auth
@rate_limit_api
async def api_keys():
    """API key usage statistics for all models."""
    try:
        from app.utils.time import get_kyiv_reset_time

        key_stats = await get_gemini_key_usage_stats()

        # Get Tavily key stats
        tavily_stats = []
        with contextlib.suppress(Exception):
            tavily_stats = await get_tavily_key_usage_stats()

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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "error_count": summary.get("error_count", 0),
                "error_rate": summary.get("error_rate_percent", 0),
                "recent_errors": list(metrics_collector.error_log)[-10:],
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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
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
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
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


# =============================================================================
# BATCH DASHBOARD ENDPOINT — 1 RTT instead of 8
# =============================================================================


async def _safe_fetch(name: str, coro):
    """Run a coroutine and return (name, result) or (name, error_dict)."""
    try:
        return name, await coro
    except Exception as e:
        logging.warning("Dashboard batch: %s failed: %s", name, e)
        return name, {"error": str(e)}


@quart_app.route("/api/dashboard")
@require_auth
@rate_limit_api
async def api_dashboard():
    """Aggregated dashboard data — all operational metrics in a single response.

    Replaces 8 separate fetch() calls on the frontend with 1 HTTP round-trip.
    Each section is fetched in parallel; individual failures are isolated.
    """
    import psutil

    from app.cache import get_multi_layer_cache_stats, ping_safe
    from app.memory_manager import get_memory_stats
    from app.metrics import metrics_collector

    # Parallel fetch of all expensive async operations
    results = await asyncio.gather(
        _safe_fetch("metrics", metrics_collector.get_metrics_summary()),
        _safe_fetch("cache", get_multi_layer_cache_stats()),
        _safe_fetch("redis_ping", ping_safe()),
        _safe_fetch("db_metrics", get_supabase_metrics()),
        _safe_fetch("keys_gemini", get_gemini_key_usage_stats()),
        _safe_fetch("keys_tavily", get_tavily_key_usage_stats()),
        _safe_fetch("keys_active", get_active_key_info()),
        return_exceptions=True,
    )

    # Collect results into a dict
    data: dict = {}
    for item in results:
        if isinstance(item, Exception):
            continue
        name, value = item
        data[name] = value

    # Sync calls (cheap, no I/O)
    try:
        system = {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "memory_percent": psutil.virtual_memory().percent,
            "memory_used_mb": round(psutil.virtual_memory().used / (1024 * 1024), 1),
            "memory_total_mb": round(psutil.virtual_memory().total / (1024 * 1024), 1),
            "disk_percent": psutil.disk_usage("/").percent,
        }
    except Exception:
        system = {}

    memory_stats = get_memory_stats()

    # Queue stats
    queue_stats = {}
    try:
        from app.queue import task_queue

        queue_stats = await task_queue.get_queue_stats()
    except Exception as e:
        logging.warning("Dashboard batch: queue failed: %s", e)

    # Circuit breakers
    breakers = {}
    try:
        from app.circuit_breaker import _circuit_breakers

        breakers = {name: cb.get_stats() for name, cb in _circuit_breakers.items()}
    except Exception:
        pass

    # Errors
    errors_data = {}
    try:
        from app.metrics import metrics_collector as mc

        summary = data.get("metrics", {})
        errors_data = {
            "error_count": summary.get("error_count", 0),
            "error_rate": summary.get("error_rate_percent", 0),
            "recent_errors": list(mc.error_log)[-10:],
        }
    except Exception:
        pass

    return jsonify(
        {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
            "overview": {
                "system": system,
                "metrics": data.get("metrics", {}),
                "services": {
                    "database": "connected" if database.is_database_connected() else "disconnected",
                    "redis": "connected" if data.get("redis_ping") else "disconnected",
                    "bot": "running",
                },
            },
            "keys": {
                "gemini": data.get("keys_gemini", {}),
                "tavily": data.get("keys_tavily", {}),
                "active": data.get("keys_active", {}),
            },
            "errors": errors_data,
            "cache": data.get("cache", {}),
            "queue": queue_stats,
            "database": data.get("db_metrics", {}),
            "circuit_breakers": breakers,
            "memory": memory_stats,
        }
    )


# ── Key health endpoint ──────────────────────────────────────────────────────


@quart_app.route("/api/key-health")
@require_auth
@rate_limit_api
async def api_key_health():
    """Expose per-key health summary for dashboard diagnostics."""
    try:
        from app.repos.keys import KeyStatusManager

        manager = KeyStatusManager()

        # key_model_status is RLS-protected — must set admin context
        async with database.db_manager.pool.acquire() as conn:
            await database.set_user_context(settings.ADMIN_ID, True, conn=conn)
            try:
                summary = await manager.get_health_summary(conn=conn)
            finally:
                await database.clear_user_context(conn=conn)

        return jsonify(
            {
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                "keys": summary,
            }
        )
    except Exception as e:
        logging.warning("Key health endpoint error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── SSE live updates ─────────────────────────────────────────────────────────


@quart_app.route("/api/events")
@require_auth
async def api_events():
    """Server-Sent Events stream for real-time dashboard updates.

    Emits a JSON payload every 5 seconds with key operational metrics.
    Client connects via `new EventSource('/api/events')`.
    """

    import psutil

    async def generate():
        while True:
            try:
                # Lightweight metrics snapshot
                payload = {
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat() + "Z",
                    "system": {
                        "cpu_percent": psutil.cpu_percent(interval=0),
                        "memory_percent": psutil.virtual_memory().percent,
                    },
                    "services": {
                        "database": "connected" if database.is_database_connected() else "disconnected",
                    },
                }

                # Queue stats (cheap)
                try:
                    from app.queue import task_queue

                    q_stats = await task_queue.get_queue_stats()
                    payload["queue"] = {
                        "pending": q_stats.get("total_pending", 0),
                        "processing": q_stats.get("processing", 0),
                    }
                except Exception:
                    pass

                # Metrics summary (cheap)
                try:
                    from app.metrics import metrics_collector as mc

                    payload["metrics"] = {
                        "total_requests": mc.request_count,
                        "error_count": len(mc.error_log),
                    }
                except Exception:
                    pass

                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                logging.warning("SSE event generation error: %s", e)
                yield f'data: {{"error": "{e}"}}\n\n'

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
    return await render_template("admin_daily.html")


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


@quart_app.route("/api/admin/dailycroc", methods=["GET"])
@require_auth
async def api_admin_dailycroc_list():
    from app import database as db

    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    rows = await db.db_query(
        """
        SELECT puzzle_date, target_word, topic, lang, difficulty, hints, image_prompt, image_file_id, image_model, prepared_at
        FROM crocodile_daily_puzzles
        ORDER BY puzzle_date DESC, difficulty ASC
        LIMIT $1
        """,
        (limit,),
    )
    
    out = []
    for r in rows:
        if r["puzzle_date"] is None:
            continue
        out.append(
            {
                "date": r["puzzle_date"].isoformat(),
                "difficulty": r["difficulty"],
                "target_word": r["target_word"],
                "topic": r["topic"],
                "image_file_id": r["image_file_id"],
                "image_prompt": r["image_prompt"],
                "image_model": r["image_model"],
            }
        )
    return jsonify({"puzzles": out})


@quart_app.route("/api/admin/dailycroc/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailycroc_regen():
    from app.repos.crocodile_daily import get_puzzle

    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    if not puzzle_date or not difficulty:
        return jsonify({"error": "missing date or difficulty"}), 400
    import datetime

    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date format"}), 400

    puzzle = await get_puzzle(dt, difficulty=difficulty)
    if not puzzle:
        return jsonify({"error": "puzzle not found"}), 404

    try:
        from app.bot_instance import get_bot

        bot = get_bot()
        if bot is None:
            return jsonify({"error": "bot not ready"}), 503

        from app.games.crocodile_daily import prepare_daily_puzzle

        updated_puzzle = await prepare_daily_puzzle(
            dt, 
            bot=bot, 
            difficulty=difficulty, 
            force_image=True
        )

        if not updated_puzzle or not updated_puzzle.image_file_id:
            return jsonify({"error": "Failed to generate or upload image"}), 500

        return jsonify({"success": True, "file_id": updated_puzzle.image_file_id})
    except Exception as e:
        logging.error("Regen failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@quart_app.route("/api/admin/dailycroc/prompt", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_prompt():
    from app.repos.crocodile_daily import set_puzzle_image_prompt

    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    prompt = data.get("prompt")
    if not puzzle_date or not difficulty or prompt is None:
        return jsonify({"error": "missing fields"}), 400
    import datetime

    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    await set_puzzle_image_prompt(dt, prompt, difficulty=difficulty)
    return jsonify({"success": True})


@quart_app.route("/api/admin/dailycroc/model", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_model():
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    model = data.get("model")
    if not puzzle_date or not difficulty or model is None:
        return jsonify({"error": "missing fields"}), 400
    import datetime

    from app.database import db_manager

    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date"}), 400

    async with db_manager.pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.crocodile_daily_puzzles
            SET image_model = $1
            WHERE puzzle_date = $2 AND difficulty = $3
            """,
            model,
            dt,
            difficulty,
        )
    return jsonify({"success": True})


@quart_app.route("/api/admin/dailycroc/reset-word", methods=["POST"])
@require_auth
async def api_admin_dailycroc_reset_word():
    from app.repos.crocodile_daily import regenerate_puzzle_word

    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    puzzle_date = data.get("date")
    difficulty = data.get("difficulty")
    if not puzzle_date or not difficulty:
        return jsonify({"error": "missing date or difficulty"}), 400
    import datetime

    try:
        dt = datetime.date.fromisoformat(puzzle_date)
    except ValueError:
        return jsonify({"error": "invalid date format"}), 400

    new_puzzle = await regenerate_puzzle_word(dt, difficulty)
    if not new_puzzle:
        return jsonify({"error": "failed to regenerate puzzle"}), 500

    return jsonify(
        {
            "success": True,
            "new_word": new_puzzle.target_word,
            "new_topic": new_puzzle.topic,
        }
    )


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
        stats["delivery_enabled"] = (delivery_on.strip().lower() != "off")

        # Get configured image model
        image_model = await get_global_setting(repo.DAILY_IMAGE_MODEL_SETTING_KEY, "pollinations")
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
    try:
        puzzle_date = datetime.date.fromisoformat(str(data.get("date") or ""))
        board = daily_2048_repo.normalize_board(data.get("board"))
        goal_type = daily_2048_repo.normalize_goal_type(str(data.get("goal_type") or "tile"))
        goal_value = int(data.get("goal_value") or 512)
        spawn_sequence = daily_2048_repo.normalize_spawn_sequence(data.get("spawn_sequence"))
        seed = str(data.get("seed") or f"custom:{puzzle_date.isoformat()}").strip()
        par_moves = int(data.get("par_moves") or daily_2048_repo.DEFAULT_PAR_MOVES)
        target_seconds = int(data.get("target_seconds") or daily_2048_repo.DEFAULT_TARGET_SECONDS)
        status = str(data.get("status") or "draft").strip().lower()
        solution_moves = str(data.get("solution_moves") or "")
    except (TypeError, ValueError):
        return jsonify({"error": "invalid puzzle payload"}), 400

    if status not in {"draft", "ready", "disabled"}:
        return jsonify({"error": "invalid status"}), 400
    if goal_value < 8:
        return jsonify({"error": "invalid goal_value"}), 400

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
    )
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


def _serialize_daily_trivia_puzzle(puzzle):
    if not puzzle:
        return None
    return {
        "date": puzzle.puzzle_date.isoformat(),
        "status": puzzle.status,
        "prepared_at": puzzle.prepared_at.isoformat() if puzzle.prepared_at else None,
        "questions": [
            {
                "id": q.id,
                "topic": q.topic,
                "question": q.question,
                "options": q.options,
                "correct_index": q.correct_index,
                "explanation": q.explanation,
            }
            for q in puzzle.questions
        ],
    }


@quart_app.route("/api/admin/dailytrivia/stats", methods=["GET"])
@require_auth
async def api_admin_dailytrivia_stats():
    from app.repos import daily_trivia as daily_trivia_repo
    from app.repos.crocodile_daily import today_puzzle_date
    from app.repos.settings_repo import get_global_setting
    today = today_puzzle_date()
    stats = await daily_trivia_repo.get_admin_stats(today)
    delivery_on = await get_global_setting("daily_trivia_delivery_enabled", "on")
    stats["delivery_enabled"] = (delivery_on.strip().lower() != "off")
    stats["llm_model"] = await get_global_setting("daily_trivia_llm_model", "gemini-economy")
    delivery_info = await daily_trivia_repo.get_delivery_status(today)
    stats["sent_today"] = delivery_info.get("sent_today", 0)
    stats["pending_today"] = delivery_info.get("pending_today", 0)
    return jsonify(stats)


@quart_app.route("/api/admin/dailytrivia/settings", methods=["POST"])
@require_auth
async def api_admin_dailytrivia_settings():
    from app.repos.settings_repo import set_global_setting
    data = await request.get_json() or {}
    if "llm_model" in data:
        model = str(data["llm_model"]).strip()
        await set_global_setting("daily_trivia_llm_model", model)
    if "delivery_enabled" in data:
        val = "on" if bool(data["delivery_enabled"]) else "off"
        await set_global_setting("daily_trivia_delivery_enabled", val)
    return jsonify({"success": True})


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
            all_puzzles.append({
                "date": curr.isoformat(),
                "status": "missing",
                "prepared_at": None,
                "questions": [],
            })
        curr += datetime.timedelta(days=1)
        
    all_puzzles.sort(key=lambda x: x["date"], reverse=True)
    
    return jsonify({
        "puzzles": all_puzzles,
        "today": today.isoformat(),
    })


@quart_app.route("/api/admin/dailytrivia/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_regenerate():
    from app.games import daily_trivia as daily_trivia_game
    from app.repos.crocodile_daily import today_puzzle_date
    data = await request.get_json() or {}
    try:
        p_date = datetime.date.fromisoformat(str(data.get("date") or today_puzzle_date()))
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    puzzle = await daily_trivia_game.prepare_daily_puzzle(p_date, force=True)
    return jsonify({"success": True, "puzzle": _serialize_daily_trivia_puzzle(puzzle)})


@quart_app.route("/api/admin/dailytrivia/save", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailytrivia_save():
    from app.repos import daily_trivia as daily_trivia_repo
    data = await request.get_json()
    if not data:
        return jsonify({"error": "invalid json"}), 400
    try:
        p_date = datetime.date.fromisoformat(str(data.get("date") or ""))
        raw_questions = data.get("questions") or []
        status = str(data.get("status") or "ready").strip().lower()
    except (TypeError, ValueError):
        return jsonify({"error": "invalid payload"}), 400

    questions = []
    for idx, q in enumerate(raw_questions):
        questions.append(
            daily_trivia_repo.TriviaQuestion(
                id=int(q.get("id", idx + 1)),
                topic=str(q.get("topic", "")).strip(),
                question=str(q.get("question", "")).strip(),
                options=[str(opt).strip() for opt in q.get("options", [])],
                correct_index=int(q.get("correct_index", 0)),
                explanation=str(q.get("explanation", "")).strip(),
            )
        )

    puzzle = await daily_trivia_repo.save_puzzle(p_date, questions, status=status)
    return jsonify({"success": True, "puzzle": _serialize_daily_trivia_puzzle(puzzle)})



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

        tarot_channel = {
            "id": "tarot",
            "name": "Карта дня (Таро)",
            "emoji": "🔮",
            "active_game": None,
            "subscribers": tarot_total,
            "pending_today": None,
            "sent_today": None,
            "delivery_enabled": tarot_enabled,
            "last_sent_at": None,
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
                where_clauses.append(
                    f"(p.last_sent_puzzle_date IS NULL OR p.last_sent_puzzle_date < ${idx})"
                )
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
                rows_out.append({
                    "user_id": r["user_id"],
                    "channels": ["🎮 Daily Challenge"],
                    "timezone": r.get("timezone") or "—",
                    "preferred_hour": r.get("preferred_local_hour"),
                    "last_sent": last_sent.isoformat() if last_sent else None,
                    "status": status,
                })

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
                    rows_out.append({
                        "user_id": uid,
                        "channels": [badge],
                        "timezone": f"UTC{r['utc_offset']:+d}" if r.get("utc_offset") is not None else "—",
                        "preferred_hour": None,
                        "last_sent": last_sent_val.isoformat() if last_sent_val else None,
                        "status": "active",
                    })

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
    """Return users with known delivery errors (stub: uses last_sent heuristic)."""
    # Full implementation requires broadcast_events table (future migration).
    # v1: return empty list with informative message.
    return jsonify({
        "errors": [],
        "note": "Detailed error log requires broadcast_events table (planned migration).",
    })


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
    from app.repos.horoscope_subscriptions import get_horoscope_subscription
    from app.repos.tarot_daily_subscriptions import get_tarot_subscription

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
                rows_out.append({
                    "user_id": r["user_id"],
                    "display_name": r.get("display_name"),
                    "channel": "daily_challenge",
                    "channel_emoji": "🎮",
                    "is_subscribed": bool(r.get("is_subscribed")),
                    "offer_sent_at": r["discovery_last_sent_at"].isoformat()
                        if r.get("discovery_last_sent_at") else None,
                    "snoozed_until": snoozed.isoformat() if snoozed else None,
                    "snoozed_active": bool(snoozed and snoozed > now),
                    "last_delivery": r["last_sent_puzzle_date"].isoformat()
                        if r.get("last_sent_puzzle_date") else None,
                })

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
                        if r.get("discovery_last_sent_at") else None,
                    "snoozed_until": None,
                    "snoozed_active": False,
                    "last_delivery": r["last_today_sent"].isoformat()
                        if r.get("last_today_sent") else None,
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
                        if r.get("discovery_last_sent_at") else None,
                    "snoozed_until": None,
                    "snoozed_active": False,
                    "last_delivery": r["last_sent_date"].isoformat()
                        if r.get("last_sent_date") else None,
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
    """Send a subscription offer to a specific user for a given channel.

    Body: { user_id: int, channel: "daily_challenge"|"horoscope"|"tarot", force: bool }

    The user must exist in public.users (bot can only message users who initiated
    a conversation). If the user is already subscribed and force != true, returns
    a warning without sending.
    """
    try:
        data = await request.get_json() or {}
        try:
            target_user_id = int(data.get("user_id") or 0)
        except (ValueError, TypeError):
            return jsonify({"error": "user_id must be an integer"}), 400

        channel = str(data.get("channel") or "").strip()
        force = bool(data.get("force", False))

        if not target_user_id:
            return jsonify({"error": "user_id is required"}), 400
        if channel not in ("daily_challenge", "horoscope", "tarot"):
            return jsonify({"error": f"unknown channel: {channel!r}"}), 400

        # Verify user is reachable (exists in users table)
        user_rows = await database.db_query(
            "SELECT user_id, username, first_name FROM public.users WHERE user_id = $1",
            (target_user_id,),
        )
        if not user_rows:
            return jsonify({
                "success": False,
                "error": "User not found. Bot can only message users who have started a conversation.",
            }), 404

        from app.bot_instance import get_bot
        bot = get_bot()
        if bot is None:
            return jsonify({"error": "Bot not available"}), 503

        # Channel-specific send logic
        if channel == "daily_challenge":
            from app.repos import crocodile_daily as croc_repo
            from app.repos.daily_2048 import get_active_daily_game_mode

            pref = await croc_repo.get_preference(target_user_id)
            already_subscribed = bool(pref and pref.get("is_subscribed"))
            if already_subscribed and not force:
                return jsonify({
                    "success": False,
                    "warning": "User is already subscribed. Pass force=true to send anyway.",
                    "already_subscribed": True,
                })

            game_mode = await get_active_daily_game_mode()
            if game_mode == "2048":
                from app.handlers.daily_2048 import send_discovery_intro as send_2048_intro
                await send_2048_intro(bot, target_user_id)
            elif game_mode == "trivia":
                from app.handlers.daily_trivia import send_discovery_intro as send_trivia_intro
                await send_trivia_intro(bot, target_user_id)
            else:
                from app.handlers.daily_crocodile import send_discovery_intro as send_croc_intro
                await send_croc_intro(bot, target_user_id)

        elif channel == "horoscope":
            from app.repos.horoscope_subscriptions import (
                get_horoscope_subscription,
                mark_horoscope_discovery_sent,
            )

            sub = await get_horoscope_subscription(target_user_id)
            already_subscribed = bool(sub and sub.get("is_active"))
            if already_subscribed and not force:
                return jsonify({
                    "success": False,
                    "warning": "User is already subscribed to horoscope. Pass force=true to send anyway.",
                    "already_subscribed": True,
                })

            from app.handlers.horoscope_subscription import send_horoscope_invite
            delivered = await send_horoscope_invite(bot, target_user_id)
            if not delivered:
                return jsonify({
                    "success": False,
                    "error": "Offer was not delivered by Telegram; discovery timestamp was not updated.",
                }), 502
            await mark_horoscope_discovery_sent(target_user_id)

        elif channel == "tarot":
            from app.repos.tarot_daily_subscriptions import (
                get_tarot_subscription,
                mark_tarot_discovery_sent,
            )

            sub = await get_tarot_subscription(target_user_id)
            already_subscribed = bool(sub and sub.get("is_subscribed"))
            if already_subscribed and not force:
                return jsonify({
                    "success": False,
                    "warning": "User is already subscribed to tarot. Pass force=true to send anyway.",
                    "already_subscribed": True,
                })

            from app.handlers.tarot_daily import send_tarot_invite
            await send_tarot_invite(bot, target_user_id)
            await mark_tarot_discovery_sent(target_user_id)

        return jsonify({
            "success": True,
            "user_id": target_user_id,
            "channel": channel,
            "message": "Offer sent successfully.",
        })
    except Exception as exc:
        logging.error("Broadcast send-offer error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/broadcast/send-offer-batch", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_broadcast_send_offer_batch():
    """Send subscription offers to multiple users for a given channel.

    Body: {
      user_ids: [int, ...],   (max 100)
      channel: "daily_challenge"|"horoscope"|"tarot",
      force: bool             (send even if already subscribed)
    }

    Uses asyncio.Semaphore(5) to avoid flooding Telegram.
    Returns per-user results with success/skip/error per entry.
    """
    try:
        data = await request.get_json() or {}
        raw_ids = data.get("user_ids") or []
        channel = str(data.get("channel") or "").strip()
        force = bool(data.get("force", False))

        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({"error": "user_ids must be a non-empty list"}), 400
        if len(raw_ids) > 100:
            return jsonify({"error": "Batch size limit is 100 users per request"}), 400
        if channel not in ("daily_challenge", "horoscope", "tarot"):
            return jsonify({"error": f"unknown channel: {channel!r}"}), 400

        try:
            user_ids = [int(uid) for uid in raw_ids]
        except (ValueError, TypeError):
            return jsonify({"error": "All user_ids must be integers"}), 400

        from app.bot_instance import get_bot
        bot = get_bot()
        if bot is None:
            return jsonify({"error": "Bot not available"}), 503

        # Verify all user IDs exist (reachable)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(user_ids)))
        reachable_rows = await database.db_query(
            f"SELECT user_id FROM public.users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        reachable_set = {r["user_id"] for r in reachable_rows}

        results: list[dict] = []
        sem = asyncio.Semaphore(5)

        async def _send_one(uid: int) -> dict:
            if uid not in reachable_set:
                return {"user_id": uid, "status": "error", "message": "User not found"}

            async with sem:
                try:
                    if channel == "daily_challenge":
                        from app.repos import crocodile_daily as croc_repo
                        from app.repos.daily_2048 import get_active_daily_game_mode

                        pref = await croc_repo.get_preference(uid)
                        if bool(pref and pref.get("is_subscribed")) and not force:
                            return {"user_id": uid, "status": "skipped", "message": "Already subscribed"}

                        game_mode = await get_active_daily_game_mode()
                        if game_mode == "2048":
                            from app.handlers.daily_2048 import send_discovery_intro as send_2048_intro
                            await send_2048_intro(bot, uid)
                        elif game_mode == "trivia":
                            from app.handlers.daily_trivia import send_discovery_intro as send_trivia_intro
                            await send_trivia_intro(bot, uid)
                        else:
                            from app.handlers.daily_crocodile import send_discovery_intro as send_croc_intro
                            await send_croc_intro(bot, uid)

                    elif channel == "horoscope":
                        from app.repos.horoscope_subscriptions import (
                            get_horoscope_subscription,
                            mark_horoscope_discovery_sent,
                        )
                        sub = await get_horoscope_subscription(uid)
                        if bool(sub and sub.get("is_active")) and not force:
                            return {"user_id": uid, "status": "skipped", "message": "Already subscribed"}
                        from app.handlers.horoscope_subscription import send_horoscope_invite
                        delivered = await send_horoscope_invite(bot, uid)
                        if not delivered:
                            return {"user_id": uid, "status": "error", "message": "Offer was not delivered"}
                        await mark_horoscope_discovery_sent(uid)

                    elif channel == "tarot":
                        from app.repos.tarot_daily_subscriptions import (
                            get_tarot_subscription,
                            mark_tarot_discovery_sent,
                        )
                        sub = await get_tarot_subscription(uid)
                        if bool(sub and sub.get("is_subscribed")) and not force:
                            return {"user_id": uid, "status": "skipped", "message": "Already subscribed"}
                        from app.handlers.tarot_daily import send_tarot_invite
                        await send_tarot_invite(bot, uid)
                        await mark_tarot_discovery_sent(uid)

                    return {"user_id": uid, "status": "sent", "message": "Offer sent"}
                except Exception as send_exc:
                    logging.warning("Batch offer send failed user=%s: %s", uid, send_exc)
                    return {"user_id": uid, "status": "error", "message": str(send_exc)}

        results = list(await asyncio.gather(*[_send_one(uid) for uid in user_ids]))

        sent = sum(1 for r in results if r["status"] == "sent")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors = sum(1 for r in results if r["status"] == "error")

        return jsonify({
            "success": True,
            "channel": channel,
            "summary": {"sent": sent, "skipped": skipped, "errors": errors},
            "results": results,
        })
    except Exception as exc:
        logging.error("Broadcast send-offer-batch error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


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

        return jsonify({
            "total_active": int(stats.get("total_active", 0)),
            "breakdown": {
                "today_only": int(stats.get("today_only", 0)),
                "tomorrow_only": int(stats.get("tomorrow_only", 0)),
                "both": int(stats.get("both_slots", 0)),
            },
            "total_inactive": int(stats.get("total_inactive", 0)),
            "by_sign": by_sign,
        })
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

        return jsonify({
            "today": today_status,
            "tomorrow": tomorrow_status,
            "subscribers": subscriber_count,
        })
    except Exception as exc:
        logging.error("Tarot status error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@quart_app.route("/api/admin/tarot/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_tarot_regenerate():
    """Force regeneration of tarot daily readings for a given date."""
    try:
        from app.tarot_daily import prepare_daily_readings

        data = await request.get_json() or {}
        date_str = str(data.get("date") or "")
        try:
            target_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            return jsonify({"error": "invalid date"}), 400

        result = await prepare_daily_readings(target_date=target_date)
        return jsonify({
            "success": True,
            "date": target_date.isoformat(),
            "generated": result.generated,
            "skipped": result.skipped,
            "failed": result.failed,
            "locked": result.locked,
        })
    except Exception as exc:
        logging.error("Tarot regenerate error: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

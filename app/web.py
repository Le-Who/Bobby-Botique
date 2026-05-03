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
from app.repos.metrics_repo import (
    get_active_key_info,
    get_gemini_key_usage_stats,
    get_supabase_metrics,
    get_tavily_key_usage_stats,
)
from app.utils.json_compat import json

# --- QUART APP SETUP ---
quart_app = Quart(__name__)  # kept as `quart_app` for backward compat with bot.py

# Register Telegram Mini App blueprint
from app.web_miniapp import miniapp_blueprint  # noqa: E402

quart_app.register_blueprint(miniapp_blueprint, url_prefix="/webapp")


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
    is_webapp = request.path.startswith("/webapp")

    if is_webapp:
        # Telegram Mini App: allow telegram.org SDK script, inline styles,
        # and framing by Telegram's WebView
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org 'unsafe-inline' "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self' wss:; "
            "frame-ancestors https://web.telegram.org https://*.telegram.org;"
        )
        # Allow Telegram to embed this page
        response.headers["X-Frame-Options"] = "ALLOWALL"
    else:
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
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
    """Check if current request has a valid session or header token."""
    # Check session cookie first
    if session.get("authenticated"):
        return True
    # Fallback: check X-Auth-Token header (for API/monitoring tools)
    token = request.headers.get("X-Auth-Token")
    expected = _get_admin_secret()
    return bool(token and expected and hmac.compare_digest(token, expected))


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


from app.security import SyncRateLimiter  # noqa: E402

_login_limiter = SyncRateLimiter(max_requests=5, window_seconds=300)
_api_limiter = SyncRateLimiter(max_requests=60, window_seconds=60)


def rate_limit_api(f):
    """Rate-limit decorator for API endpoints (60 req/min per IP)."""

    @wraps(f)
    async def decorated(*args, **kwargs):
        client_ip = request.remote_addr or "unknown"
        if not _api_limiter.check(client_ip):
            return jsonify({"error": "Rate limit exceeded"}), 429
        return await f(*args, **kwargs)

    return decorated


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


# ── Admin Daily Crocodile Dashboard ───────────────────────────────────


@quart_app.route("/admin_dailycroc")
@require_auth
async def admin_dailycroc_page():
    """Serve the Daily Crocodile Admin Dashboard."""
    return await render_template("admin_dailycroc.html")


@quart_app.route("/api/admin/dailycroc", methods=["GET"])
@require_auth
async def api_admin_dailycroc_list():
    from app import database as db
    from app.repos.crocodile_daily import _row_to_puzzle

    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20

    rows = await db.fetch_all(
        """
        SELECT puzzle_date, target_word, topic, lang, difficulty, hints, image_prompt, image_file_id, image_model, prepared_at
        FROM crocodile_daily_puzzles
        ORDER BY puzzle_date DESC, difficulty ASC
        LIMIT $1
        """,
        limit,
    )
    puzzles = [_row_to_puzzle(r) for r in rows]
    out = []
    for puzzle in puzzles:
        if puzzle.puzzle_date is None:
            continue
        out.append(
            {
                "date": puzzle.puzzle_date.isoformat(),
                "difficulty": puzzle.difficulty,
                "target_word": puzzle.target_word,
                "topic": puzzle.topic,
                "image_file_id": puzzle.image_file_id,
                "image_prompt": puzzle.image_prompt,
                "image_model": puzzle.image_model,
            }
        )
    return jsonify({"puzzles": out})


@quart_app.route("/api/admin/dailycroc/regenerate", methods=["POST"])
@require_auth
@rate_limit_api
async def api_admin_dailycroc_regen():
    from app.providers.pollinations import generate_image_model
    from app.repos.crocodile_daily import get_daily_puzzle_strict, set_puzzle_image_asset

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

    puzzle = await get_daily_puzzle_strict(dt, difficulty)
    if not puzzle:
        return jsonify({"error": "puzzle not found"}), 404

    try:
        from telegram import InputMediaPhoto

        from app.bot_instance import get_bot

        bot = get_bot()
        if bot is None:
            return jsonify({"error": "bot not ready"}), 503

        model = puzzle.image_model or "zimage"
        photo_bytes = await generate_image_model(puzzle.image_prompt, width=1024, height=1024, model=model)

        # Send to config group to get file_id
        from app.config import settings

        msg = await bot.send_photo(chat_id=settings.CONFIG_CHAT_ID, photo=photo_bytes)
        file_id = msg.photo[-1].file_id

        await set_puzzle_image_asset(dt, file_id, difficulty=difficulty)
        return jsonify({"success": True, "file_id": file_id})
    except Exception as e:
        logging.error("Regen failed: %s", e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@quart_app.route("/api/admin/dailycroc/prompt", methods=["POST"])
@require_auth
async def api_admin_dailycroc_update_prompt():
    from app.repos.crocodile_daily import update_daily_puzzle_prompt

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

    await update_daily_puzzle_prompt(dt, difficulty, prompt)
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

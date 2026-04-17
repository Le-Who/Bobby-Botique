# /app/admin_alerts.py
"""Admin alert system — rate-limited Telegram notifications for critical events.

Sends alerts to the bot admin (ADMIN_ID) when critical errors occur,
with rate limiting to prevent spam during cascading failures.
"""

from __future__ import annotations

import logging
import time
import traceback
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels."""

    INFO = "ℹ️"
    WARNING = "⚠️"
    CRITICAL = "🚨"


# ── Rate limiter ─────────────────────────────────────────────────────────────

_alert_timestamps: list[float] = []
_MAX_ALERTS = 5  # max alerts per window
_WINDOW_SECONDS = 300.0  # 5 minutes


def _is_rate_limited() -> bool:
    """Check if we've exceeded the alert rate limit."""
    now = time.monotonic()
    # Purge old entries
    while _alert_timestamps and now - _alert_timestamps[0] > _WINDOW_SECONDS:
        _alert_timestamps.pop(0)
    return len(_alert_timestamps) >= _MAX_ALERTS


def _record_alert() -> None:
    _alert_timestamps.append(time.monotonic())


# ── Public API ───────────────────────────────────────────────────────────────


async def alert_admin(
    app: Application,
    message: str,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    exc: BaseException | None = None,
) -> None:
    """Send a rate-limited alert to the bot admin via Telegram.

    Args:
        app: The PTB Application instance (needed for bot.send_message).
        message: Human-readable description of the issue.
        severity: Alert severity level.
        exc: Optional exception to include traceback for.
    """
    if _is_rate_limited():
        logger.debug("Admin alert rate-limited, dropping: %s", message)
        return

    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    # Build alert text
    parts = [
        f"{severity.value} *{severity.name}*",
        "",
        message,
    ]

    if exc:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        # Truncate to avoid Telegram 4096-char limit
        if len(tb) > 2000:
            tb = tb[:1000] + "\n...\n" + tb[-800:]
        parts.append(f"\n```\n{tb}```")

    text = "\n".join(parts)
    # Hard cap at Telegram limit
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    try:
        from app.utils.formatting import TelegramFormatter

        fmt_text, fmt_pm = TelegramFormatter.format_text(text)
        await app.bot.send_message(
            chat_id=admin_id,
            text=fmt_text,
            parse_mode=fmt_pm,
        )
        _record_alert()
        logger.info("Admin alert sent: %s", message[:80])
    except Exception as send_err:
        # Never let alerting crash the main flow
        logger.warning("Failed to send admin alert: %s", send_err)


async def alert_admin_shutdown(app: Application, reason: str = "normal") -> None:
    """Send a shutdown notification to admin (bypasses rate limiter)."""
    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    text = f"🔴 *Бот остановлен*\nПричина: {reason}"
    try:
        from app.utils.formatting import TelegramFormatter

        fmt_text, fmt_pm = TelegramFormatter.format_text(text)
        await app.bot.send_message(
            chat_id=admin_id,
            text=fmt_text,
            parse_mode=fmt_pm,
        )
    except Exception:
        pass  # Best-effort on shutdown


async def alert_admin_startup(app: Application) -> None:
    """Send a startup notification to admin (bypasses rate limiter)."""
    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    from app.degradation import check_system_health

    health = await check_system_health()

    status_emoji = "🟢" if health.overall.value == "healthy" else "🟡"
    text = (
        f"🟢 *Бот запущен*\n"
        f"Статус: {status_emoji} {health.overall.value}\n"
        f"DB: {health.database.value} | Redis: {health.redis.value} | AI: {health.ai_provider.value}"
    )
    try:
        from app.utils.formatting import TelegramFormatter

        fmt_text, fmt_pm = TelegramFormatter.format_text(text)
        await app.bot.send_message(
            chat_id=admin_id,
            text=fmt_text,
            parse_mode=fmt_pm,
        )
    except Exception:
        pass  # Best-effort on startup


async def alert_admin_raw(
    message: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
) -> None:
    """Send an alert using the bot singleton — no Application instance needed.

    Useful for alerts emitted before or after the PTB Application lifecycle
    (e.g. migration drift detected in database.py during init_db()).
    Falls back to logging if the bot singleton is not yet registered.
    """
    if _is_rate_limited():
        logger.debug("Admin alert rate-limited, dropping: %s", message)
        return

    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    parts = [f"{severity.value} *{severity.name}*", "", message]
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    try:
        from app.bot_instance import get_bot

        bot = get_bot()
        if bot is None:
            logger.warning("alert_admin_raw: bot not yet registered — logging only: %s", message)
            return

        from app.utils.formatting import TelegramFormatter

        fmt_text, fmt_pm = TelegramFormatter.format_text(text)
        await bot.send_message(chat_id=admin_id, text=fmt_text, parse_mode=fmt_pm)
        _record_alert()
        logger.info("Admin alert (raw) sent: %s", message[:80])
    except Exception as send_err:
        logger.warning("Failed to send raw admin alert: %s", send_err)


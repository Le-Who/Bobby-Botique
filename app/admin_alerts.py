# /app/admin_alerts.py
"""Admin alert system — rate-limited Telegram notifications for critical events.

Sends alerts to the bot admin (ADMIN_ID) when critical errors occur,
with rate limiting to prevent spam during cascading failures.
"""

from __future__ import annotations

import hashlib
import logging
import time
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

logger = logging.getLogger(__name__)


def _safe_alert_message(message: str) -> tuple[str, str]:
    from app.observability.redaction import sanitize_event

    sanitized = sanitize_event({"message": message})["message"]
    safe_message = sanitized if isinstance(sanitized, str) else "[unavailable]"
    fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return safe_message, fingerprint


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


# ── Unauthorized user alert cooldown ─────────────────────────────────────────
# Per-user cooldown prevents alert spam when a user writes multiple messages.

_unauthorized_last_alerted: dict[int, float] = {}
_UNAUTHORIZED_COOLDOWN_S = 600.0  # 10 minutes per user


def _can_alert_unauthorized(user_id: int) -> bool:
    """Return True if enough time has passed since last alert for this user."""
    now = time.monotonic()
    last = _unauthorized_last_alerted.get(user_id)
    return last is None or (now - last) >= _UNAUTHORIZED_COOLDOWN_S


def _record_unauthorized_alert(user_id: int) -> None:
    _unauthorized_last_alerted[user_id] = time.monotonic()


# ── Public API ───────────────────────────────────────────────────────────────


async def alert_admin(
    app: Application,
    message: str,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    exc: BaseException | None = None,
    error_id: str | None = None,
) -> None:
    """Send a rate-limited alert to the bot admin via Telegram.

    Args:
        app: The PTB Application instance (needed for bot.send_message).
        message: Human-readable description of the issue.
        severity: Alert severity level.
        exc: Optional exception to summarize without exposing its raw message.
        error_id: Correlation identifier from the primary structured incident.
    """
    safe_message, alert_fingerprint = _safe_alert_message(message)
    if _is_rate_limited():
        logger.debug(
            "Admin alert rate-limited",
            extra={"_event_name": "alert.rate_limited", "alert_fingerprint": alert_fingerprint},
        )
        return

    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    # Build alert text
    parts = [
        f"{severity.value} *{severity.name}*",
        "",
        safe_message,
        f"Alert fingerprint: `{alert_fingerprint}`",
    ]

    if exc:
        from app.observability.schema import serialize_exception

        snapshot = serialize_exception(exc)
        parts.append(f"Exception: `{snapshot['type']}`")
        fingerprint = snapshot.get("message_fingerprint")
        if isinstance(fingerprint, str):
            parts.append(f"Exception fingerprint: `{fingerprint}`")
        stack = snapshot.get("stack")
        if isinstance(stack, list) and stack:
            locations = []
            for frame in stack[-8:]:
                if isinstance(frame, dict):
                    locations.append(f"{frame.get('file')}:{frame.get('line')}:{frame.get('function')}")
            if locations:
                parts.append("Stack locations:\n```\n" + "\n".join(locations) + "\n```")
    if error_id:
        parts.append(f"Error ID: `{error_id}`")

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
        logger.info(
            "Admin alert sent",
            extra={
                "_event_name": "alert.sent",
                "alert_fingerprint": alert_fingerprint,
                "error_id": error_id,
            },
        )
    except Exception:
        # Never let alerting crash the main flow
        logger.warning(
            "Failed to send admin alert",
            extra={"_event_name": "alert.delivery_failed", "alert_fingerprint": alert_fingerprint},
            exc_info=True,
        )


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
    safe_message, alert_fingerprint = _safe_alert_message(message)
    if _is_rate_limited():
        logger.debug(
            "Admin alert rate-limited",
            extra={"_event_name": "alert.rate_limited", "alert_fingerprint": alert_fingerprint},
        )
        return

    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    parts = [f"{severity.value} *{severity.name}*", "", safe_message, f"Alert fingerprint: `{alert_fingerprint}`"]
    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    try:
        from app.bot_instance import get_bot

        bot = get_bot()
        if bot is None:
            logger.warning(
                "alert_admin_raw: bot not yet registered",
                extra={"_event_name": "alert.bot_unavailable", "alert_fingerprint": alert_fingerprint},
            )
            return

        from app.utils.formatting import TelegramFormatter

        fmt_text, fmt_pm = TelegramFormatter.format_text(text)
        await bot.send_message(chat_id=admin_id, text=fmt_text, parse_mode=fmt_pm)
        _record_alert()
        logger.info(
            "Admin alert (raw) sent",
            extra={"_event_name": "alert.sent", "alert_fingerprint": alert_fingerprint},
        )
    except Exception:
        logger.warning(
            "Failed to send raw admin alert",
            extra={"_event_name": "alert.delivery_failed", "alert_fingerprint": alert_fingerprint},
            exc_info=True,
        )


async def alert_admin_unauthorized_user(
    app: Application,
    user_id: int,
    username: str | None,
    first_name: str | None,
    language_code: str | None,
    chat_type: str,
    message_text: str | None,
) -> None:
    """Send an alert to the admin about an unauthorized user attempt."""
    if not _can_alert_unauthorized(user_id):
        return

    from app.config import settings

    admin_id = settings.ADMIN_ID
    if not admin_id:
        return

    import html

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    name = html.escape(first_name or "Unknown")
    user_handle = f"@{html.escape(username)}" if username else "No username"
    lang = html.escape(language_code or "unknown")
    content = message_text or ""
    content_fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    message = (
        f"🚨 <b>Попытка доступа от неавторизованного пользователя</b>\n\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>Username:</b> {user_handle}\n"
        f"<b>Язык:</b> {lang}\n"
        f"<b>Тип чата:</b> {chat_type}\n\n"
        f"<b>Длина сообщения:</b> {len(content)}\n"
        f"<b>Message fingerprint:</b> <code>{content_fingerprint}</code>"
    )

    # Note: url="tg://user?id=USER_ID" opens the user's profile in Telegram clients
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Добавить в whitelist", callback_data=f"unauthorized_add:{user_id}"),
            ],
            [
                InlineKeyboardButton("👤 Профиль", url=f"tg://user?id={user_id}"),
                InlineKeyboardButton("🚫 Игнорировать", callback_data=f"unauthorized_dismiss:{user_id}"),
            ],
        ]
    )

    try:
        await app.bot.send_message(
            chat_id=admin_id,
            text=message,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        _record_unauthorized_alert(user_id)
        logger.info("Admin alert sent for unauthorized user %s", user_id)
    except Exception:
        logger.warning(
            "Failed to send unauthorized user alert",
            extra={
                "_event_name": "alert.delivery_failed",
                "actor_user_id": user_id,
                "content_fingerprint": content_fingerprint,
            },
            exc_info=True,
        )

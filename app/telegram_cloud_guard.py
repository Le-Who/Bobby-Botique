from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from telegram import Bot
from telegram.request import HTTPXRequest


@dataclass(frozen=True)
class CloudBotApiReleaseResult:
    ok: bool
    status: str
    webhook_was_active: bool = False
    cloud_webhook_still_active: bool = False
    delete_webhook_called: bool = False
    log_out_called: bool = False
    pending_update_count: int | None = None
    error: str | None = None


def _build_cloud_bot(token: str) -> Bot:
    request = HTTPXRequest(
        connection_pool_size=2,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
    )
    return Bot(token=token, request=request)


def _is_already_released_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "logged out" in message or "log out" in message


async def release_cloud_bot_api_session(
    token: str,
    *,
    bot_factory: Callable[[str], Any] | None = None,
    drop_pending_updates: bool = True,
    logger: logging.Logger | None = None,
) -> CloudBotApiReleaseResult:
    """Release the official cloud Bot API side before using a local Bot API server.

    A self-hosted telegram-bot-api server has its own webhook state. If the official
    cloud Bot API keeps an old webhook, both backends can deliver the same user action.
    This guard is intentionally idempotent and treats an already logged-out cloud side
    as success.
    """

    log = logger or logging.getLogger(__name__)
    factory = bot_factory or _build_cloud_bot
    delete_called = False
    log_out_called = False
    webhook_was_active = False
    pending_update_count: int | None = None

    try:
        async with factory(token) as bot:
            try:
                before = await bot.get_webhook_info()
            except Exception as exc:
                if _is_already_released_error(exc):
                    log.info("Official Telegram cloud Bot API is already released.")
                    return CloudBotApiReleaseResult(ok=True, status="cloud_already_released")
                return CloudBotApiReleaseResult(
                    ok=False,
                    status="cloud_probe_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

            webhook_was_active = bool(getattr(before, "url", ""))
            pending_update_count = getattr(before, "pending_update_count", None)
            if webhook_was_active:
                delete_called = True
                log.warning(
                    "Official Telegram cloud webhook is active while local Bot API is configured; deleting it."
                )
                await bot.delete_webhook(drop_pending_updates=drop_pending_updates)

            log_out_called = True
            try:
                await bot.log_out()
            except Exception as exc:
                if _is_already_released_error(exc):
                    log.info("Official Telegram cloud Bot API logout reports it is already released.")
                    return CloudBotApiReleaseResult(
                        ok=True,
                        status="cloud_already_released",
                        webhook_was_active=webhook_was_active,
                        delete_webhook_called=delete_called,
                        log_out_called=log_out_called,
                        pending_update_count=pending_update_count,
                    )
                return CloudBotApiReleaseResult(
                    ok=False,
                    status="cloud_logout_failed",
                    webhook_was_active=webhook_was_active,
                    delete_webhook_called=delete_called,
                    log_out_called=log_out_called,
                    pending_update_count=pending_update_count,
                    error=f"{type(exc).__name__}: {exc}",
                )

            try:
                after = await bot.get_webhook_info()
            except Exception as exc:
                if _is_already_released_error(exc):
                    return CloudBotApiReleaseResult(
                        ok=True,
                        status="cloud_released",
                        webhook_was_active=webhook_was_active,
                        delete_webhook_called=delete_called,
                        log_out_called=log_out_called,
                        pending_update_count=pending_update_count,
                    )
                return CloudBotApiReleaseResult(
                    ok=False,
                    status="cloud_verify_failed",
                    webhook_was_active=webhook_was_active,
                    delete_webhook_called=delete_called,
                    log_out_called=log_out_called,
                    pending_update_count=pending_update_count,
                    error=f"{type(exc).__name__}: {exc}",
                )

            still_active = bool(getattr(after, "url", ""))
            return CloudBotApiReleaseResult(
                ok=not still_active,
                status="cloud_webhook_still_active" if still_active else "cloud_released",
                webhook_was_active=webhook_was_active,
                cloud_webhook_still_active=still_active,
                delete_webhook_called=delete_called,
                log_out_called=log_out_called,
                pending_update_count=pending_update_count,
            )
    except Exception as exc:
        return CloudBotApiReleaseResult(
            ok=False,
            status="cloud_release_failed",
            webhook_was_active=webhook_was_active,
            delete_webhook_called=delete_called,
            log_out_called=log_out_called,
            pending_update_count=pending_update_count,
            error=f"{type(exc).__name__}: {exc}",
        )

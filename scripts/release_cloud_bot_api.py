from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.observability.redaction import register_sensitive_credential
from app.observability.workload_events import start_workload_attempt
from app.telegram_cloud_guard import release_cloud_bot_api_session
from app.utils.logging_config import setup_detailed_logging, shutdown_detailed_logging


async def _run() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logging.critical("TELEGRAM_BOT_TOKEN is required to release official Telegram cloud Bot API state.")
        return 1

    attempt = start_workload_attempt(
        workload="telegram_cloud_release",
        provider="telegram",
        model=None,
        api_key=None,
        origin="deployment_guard",
        **register_sensitive_credential("bot_token", token),
    )
    try:
        result = await release_cloud_bot_api_session(token)
    except Exception as error:
        attempt.fail(error, reason_code="request_failed")
        return 1
    if not result.ok:
        logging.critical(
            "Official Telegram cloud Bot API release failed: status=%s error=%s webhook_was_active=%s "
            "delete_webhook_called=%s log_out_called=%s pending_update_count=%s",
            result.status,
            result.error,
            result.webhook_was_active,
            result.delete_webhook_called,
            result.log_out_called,
            result.pending_update_count,
        )
        attempt.finish(
            outcome="failed",
            level="error",
            reason_code=result.status,
            webhook_was_active=result.webhook_was_active,
            delete_webhook_called=result.delete_webhook_called,
            log_out_called=result.log_out_called,
            pending_update_count=result.pending_update_count,
        )
        return 1

    logging.info(
        "Official Telegram cloud Bot API release OK: status=%s webhook_was_active=%s "
        "delete_webhook_called=%s log_out_called=%s pending_update_count=%s",
        result.status,
        result.webhook_was_active,
        result.delete_webhook_called,
        result.log_out_called,
        result.pending_update_count,
    )
    attempt.finish(
        outcome="succeeded",
        webhook_was_active=result.webhook_was_active,
        delete_webhook_called=result.delete_webhook_called,
        log_out_called=result.log_out_called,
        pending_update_count=result.pending_update_count,
    )
    return 0


if __name__ == "__main__":
    setup_detailed_logging(log_to_file=False)
    try:
        sys.exit(asyncio.run(_run()))
    finally:
        shutdown_detailed_logging()

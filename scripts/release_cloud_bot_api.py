from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.telegram_cloud_guard import release_cloud_bot_api_session


async def _run() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logging.critical("TELEGRAM_BOT_TOKEN is required to release official Telegram cloud Bot API state.")
        return 1

    result = await release_cloud_bot_api_session(token)
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
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    sys.exit(asyncio.run(_run()))

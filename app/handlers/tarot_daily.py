from __future__ import annotations

import logging
from datetime import timedelta

from telegram.ext import ContextTypes

from app.tarot_daily import is_preparation_window, prepare_daily_readings, today_reading_date

logger = logging.getLogger(__name__)


async def check_tarot_daily_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_preparation_window():
        return
    try:
        today = today_reading_date()
        for target_date in (today, today + timedelta(days=1)):
            result = await prepare_daily_readings(target_date=target_date)
            logger.info(
                "Tarot daily prep finished date=%s generated=%d skipped=%d failed=%d locked=%s",
                result.target_date,
                result.generated,
                result.skipped,
                result.failed,
                result.locked,
            )
    except Exception as exc:
        logger.warning("Tarot daily prep job failed: %s", exc, exc_info=True)

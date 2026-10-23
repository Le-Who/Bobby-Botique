"""
Repository for per-user statistics queries.
"""

from datetime import datetime, timedelta
from typing import Any

from app import database as db
from app.utils.time import get_kyiv_tz


async def get_user_today_request_count(user_id: int) -> int:
    """Return the user's count for the Kyiv day used by metric events."""
    result = await db.db_query(
        "SELECT COALESCE(request_count, 0) as cnt FROM user_metrics WHERE user_id = $1 AND metric_date = $2",
        (user_id, datetime.now(get_kyiv_tz()).date()),
    )
    return result[0]["cnt"] if result else 0


async def get_user_weekly_stats(user_id: int) -> list[dict[str, Any]]:
    """Returns per-day request counts for the last 7 days."""
    today = datetime.now(get_kyiv_tz()).date()
    return await db.db_query(
        "SELECT metric_date, request_count as cnt "
        "FROM user_metrics WHERE user_id = $1 AND metric_date BETWEEN $2 AND $3 "
        "ORDER BY metric_date",
        (user_id, today - timedelta(days=6), today),
    )


async def get_user_model_usage_today(user_id: int) -> list[dict[str, Any]]:
    """Returns model usage breakdown for today."""
    return await db.db_query(
        "SELECT key as model_name, value::int as cnt "
        "FROM user_metrics, jsonb_each_text(model_usage) "
        "WHERE user_id = $1 AND metric_date = $2 "
        "ORDER BY value::int DESC",
        (user_id, datetime.now(get_kyiv_tz()).date()),
    )

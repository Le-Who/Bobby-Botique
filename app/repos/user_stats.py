"""
Repository for per-user statistics queries.
"""

from typing import Any

from app import database as db


async def get_user_today_request_count(user_id: int) -> int:
    """Returns today's request count for a user."""
    result = await db.db_query(
        "SELECT COALESCE(request_count, 0) as cnt FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
        (user_id,),
    )
    return result[0]["cnt"] if result else 0


async def get_user_weekly_stats(user_id: int) -> list[dict[str, Any]]:
    """Returns per-day request counts for the last 7 days."""
    return await db.db_query(
        "SELECT metric_date, request_count as cnt "
        "FROM user_metrics WHERE user_id = $1 AND metric_date >= CURRENT_DATE - INTERVAL '6 days' "
        "ORDER BY metric_date",
        (user_id,),
    )


async def get_user_model_usage_today(user_id: int) -> list[dict[str, Any]]:
    """Returns model usage breakdown for today."""
    return await db.db_query(
        "SELECT key as model_name, value::int as cnt "
        "FROM user_metrics, jsonb_each_text(model_usage) "
        "WHERE user_id = $1 AND metric_date = CURRENT_DATE "
        "ORDER BY value::int DESC",
        (user_id,),
    )

async def get_user_activity_summary(user_id: int) -> tuple[int, int, int]:
    """Returns today's requests, documents count, and conversations count in one query."""
    query = """
    WITH req AS (
        SELECT COALESCE(request_count, 0) as cnt
        FROM user_metrics
        WHERE user_id = $1 AND metric_date = CURRENT_DATE
    ),
    doc AS (
        SELECT COUNT(*) as cnt
        FROM user_documents
        WHERE user_id = $1
    ),
    conv AS (
        SELECT COUNT(*) as cnt
        FROM public.conversations
        WHERE user_id = $1
    )
    SELECT
        COALESCE((SELECT cnt FROM req), 0) as req_count,
        (SELECT cnt FROM doc) as doc_count,
        (SELECT cnt FROM conv) as conv_count
    """
    result = await db.db_query(query, (user_id,))
    if result:
        return result[0]["req_count"], result[0]["doc_count"], result[0]["conv_count"]
    return 0, 0, 0

"""
Repository for per-user statistics queries.
"""

from typing import Any

from app import database as db


async def get_user_activity_summary(user_id: int) -> dict[str, int]:
    """Returns today's requests, document count, and conversation count in a single query."""
    query = """
        SELECT
            (SELECT COALESCE(request_count, 0) FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE) as today_requests,
            (SELECT COUNT(*) FROM user_documents WHERE user_id = $1) as doc_count,
            (SELECT COUNT(*) FROM conversations WHERE user_id = $1) as conv_count
    """
    result = await db.db_query(query, (user_id,))
    if result:
        row = result[0]
        return {
            "today_requests": row["today_requests"] or 0,
            "doc_count": row["doc_count"] or 0,
            "conv_count": row["conv_count"] or 0,
        }
    return {"today_requests": 0, "doc_count": 0, "conv_count": 0}


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

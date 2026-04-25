"""
Repository for per-user statistics queries.
"""

from typing import Any

from app import database as db


async def get_user_activity_summary(user_id: int) -> dict[str, int]:
    """
    Returns a summary of user activity (request count today, total documents, total conversations)
    in a single atomic query to avoid multiple network roundtrips.
    """
    query = """
        SELECT
            (SELECT COALESCE(request_count, 0) FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE) as request_count,
            (SELECT COUNT(*) FROM user_documents WHERE user_id = $1) as document_count,
            (SELECT COUNT(*) FROM public.conversations WHERE user_id = $1) as conversation_count
    """
    result = await db.db_query(query, (user_id,))

    if result and result[0]:
        return {
            "request_count": result[0]["request_count"] or 0,
            "document_count": result[0]["document_count"] or 0,
            "conversation_count": result[0]["conversation_count"] or 0,
        }

    return {
        "request_count": 0,
        "document_count": 0,
        "conversation_count": 0,
    }


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

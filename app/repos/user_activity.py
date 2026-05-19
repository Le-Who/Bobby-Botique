"""
Repository for consolidated user activity queries.
"""

import logging

from app import database as db


async def get_user_activity_summary(user_id: int) -> dict[str, int]:
    """Returns a summary of user activity (requests today, documents, conversations) in a single query."""
    query = """
    SELECT
        COALESCE((SELECT request_count FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE LIMIT 1), 0) as req_count,
        (SELECT COUNT(*) FROM user_documents WHERE user_id = $1) as doc_count,
        (SELECT COUNT(*) FROM public.conversations WHERE user_id = $1) as conv_count
    """
    try:
        result = await db.db_query(query, (user_id,))
        if result:
            return {
                "req_count": result[0]["req_count"],
                "doc_count": result[0]["doc_count"],
                "conv_count": result[0]["conv_count"]
            }
    except Exception as e:
        logging.error(f"Error fetching user activity summary: {e}")

    return {"req_count": 0, "doc_count": 0, "conv_count": 0}

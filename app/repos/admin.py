"""
Repository for admin-only operations.
"""

from typing import List, Dict, Any

from app import database as db


async def authorize_user(user_id: int) -> None:
    """Grants authorization to a user (upsert)."""
    await db.db_query(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) "
        "ON CONFLICT (user_id) DO UPDATE SET is_authorized = 1",
        (user_id,),
    )


async def revoke_user(user_id: int) -> None:
    """Revokes authorization for a user."""
    await db.db_query(
        "UPDATE users SET is_authorized = 0 WHERE user_id = $1", (user_id,)
    )


async def list_authorized_users() -> List[int]:
    """Returns all authorized user IDs."""
    rows = await db.db_query("SELECT user_id FROM users WHERE is_authorized = 1")
    return [row["user_id"] for row in rows]


async def clear_old_metrics() -> None:
    """Deletes metrics older than 30 days and errors older than 7 days."""
    await db.db_query("""
        DELETE FROM metrics 
        WHERE metric_date < CURRENT_DATE - INTERVAL '30 days'
    """)
    await db.db_query("""
        DELETE FROM error_logs 
        WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
    """)


async def get_all_tavily_keys() -> List[Dict[str, Any]]:
    """Returns all Tavily API keys (admin display)."""
    return await db.db_query("SELECT key_hash, api_key FROM tavily_api_keys")


async def get_tavily_usage_for_month(month_str: str) -> List[Dict[str, Any]]:
    """Returns Tavily key usage for a given month."""
    return await db.db_query(
        """
        SELECT key_hash, credit_usage
        FROM tavily_key_usage 
        WHERE usage_month = $1
        """,
        (month_str,),
    )

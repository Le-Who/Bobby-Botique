"""
Database and API key metrics/stats queries for the monitoring dashboard.

Extracted from app/database.py to isolate observability queries.
"""

import time
from datetime import date, datetime
from typing import Any

import asyncpg

from app.config import settings
from app.database import (
    clear_user_context,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.utils.time import get_pacific_tz


async def optimize_database_connections() -> bool:
    if not db_manager.pool:
        return False
    try:
        async with db_manager.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '60s'")
            await conn.execute("SET idle_in_transaction_session_timeout = '30s'")
            await conn.execute("SET lock_timeout = '30s'")
        return True
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def get_supabase_metrics() -> dict[str, Any]:
    if not db_manager.pool:
        return {"status": "disconnected", "pool_size": 0, "active_connections": 0}
    try:
        pool = db_manager.pool
        pool_size = getattr(pool, "_size", 0)
        free_size = getattr(pool, "_free_size", 0)
        pool_stats = {
            "status": "connected" if not db_manager._is_pool_closed() else "closed",
            "pool_size": pool_size,
            "free_size": free_size,
            "active_connections": pool_size - free_size,
        }
        start_time = time.time()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
            response_time = time.time() - start_time
            pool_stats.update(
                {
                    "response_time_ms": round(response_time * 1000, 2),
                    "connection_health": "healthy" if response_time < 0.1 else "slow",
                }
            )
        return pool_stats
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        return {
            "status": "error",
            "error": str(e),
            "pool_size": 0,
            "active_connections": 0,
        }


async def get_tavily_key_usage_stats() -> list[dict[str, Any]]:
    """Get monthly credit usage stats for all Tavily API keys."""
    current_month = datetime.now(get_pacific_tz()).strftime("%Y-%m")
    query = """
        SELECT
            ak.key_hash,
            LEFT(ak.key_hash, 8) || '***' as api_key_preview,
            COALESCE(ku.credit_usage, 0) as credit_usage,
            $1::int as credit_limit,
            CASE
                WHEN $1::int = 0 THEN 0
                ELSE (COALESCE(ku.credit_usage, 0)::float / $1::int * 100)
            END as usage_percent,
            CASE
                WHEN $1::int = 0 THEN true
                ELSE COALESCE(ku.credit_usage, 0) < ($1::int * $2)
            END as is_available
        FROM tavily_api_keys ak
        LEFT JOIN tavily_key_usage ku ON ak.key_hash = ku.key_hash
            AND ku.usage_month = $3
        ORDER BY COALESCE(ku.credit_usage, 0) ASC
    """
    return await db_query(
        query,
        (
            settings.TAVILY_MONTHLY_CREDIT_LIMIT,
            settings.TAVILY_LIMIT_THRESHOLD_PERCENT,
            current_month,
        ),
    )


async def get_gemini_key_usage_stats(
    model_name: str | None = None,
) -> list[dict[str, Any]]:
    today_pacific: date = datetime.now(get_pacific_tz()).date()
    if model_name:
        query = """
            SELECT
                ak.key_hash,
                LEFT(ak.key_hash, 8) || '***' as api_key_preview,
                COALESCE(ku.request_count, 0) as request_count,
                mc.daily_limit,
                CASE
                    WHEN mc.daily_limit IS NULL THEN 0
                    ELSE (COALESCE(ku.request_count, 0)::float / mc.daily_limit * 100)
                END as usage_percent,
                CASE
                    WHEN mc.daily_limit IS NULL THEN true
                    ELSE COALESCE(ku.request_count, 0) < (mc.daily_limit * $2)
                END as is_available
            FROM public.api_keys ak
            LEFT JOIN public.model_configuration mc ON mc.model_name = $1
            LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash
                AND ku.model_name = $1 AND ku.usage_date = $3
            ORDER BY COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(
            query,
            (
                model_name,
                settings.LIMIT_THRESHOLD_PERCENT,
                today_pacific,
            ),
        )
    else:
        query = """
            SELECT
                ak.key_hash,
                LEFT(ak.key_hash, 8) || '***' as api_key_preview,
                ku.model_name,
                COALESCE(ku.request_count, 0) as request_count,
                mc.daily_limit,
                CASE
                    WHEN mc.daily_limit IS NULL THEN 0
                    ELSE (COALESCE(ku.request_count, 0)::float / mc.daily_limit * 100)
                END as usage_percent,
                CASE
                    WHEN mc.daily_limit IS NULL THEN true
                    ELSE COALESCE(ku.request_count, 0) < (mc.daily_limit * $1)
                END as is_available
            FROM public.api_keys ak
            LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash AND ku.usage_date = $2
            LEFT JOIN public.model_configuration mc ON mc.model_name = ku.model_name
            WHERE ku.model_name IS NOT NULL
            ORDER BY ku.model_name, COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (settings.LIMIT_THRESHOLD_PERCENT, today_pacific))
    return results


async def get_active_key_info(model_name: str) -> dict[str, Any] | None:
    cached_key = None
    if model_name in db_manager._active_keys_cache:
        cached_key = db_manager._active_keys_cache[model_name]

    if not cached_key:
        return None

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            from app.repos.keys import _is_key_available

            is_available = await _is_key_available(cached_key["key_hash"], model_name, conn=conn)
            return {
                "key_hash": cached_key["key_hash"],
                "api_key_preview": cached_key["key_hash"][:8] + "***",
                "is_available": is_available,
                "cached_at": time.time(),
            }
        finally:
            await clear_user_context(conn=conn)

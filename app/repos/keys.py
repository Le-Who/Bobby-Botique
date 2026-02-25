"""
API key management for Gemini, OpenRouter, and Tavily providers.

Handles key rotation, usage tracking, cache invalidation,
and daily/monthly limit enforcement.

Extracted from app/database.py to isolate key-management domain logic.
"""

import hashlib
import logging
import time
from datetime import datetime, date
from typing import Dict, Any, Optional

from app.config import UTC_TZ, settings
from app.utils.time import get_pacific_tz
from app.database import (
    db_manager,
    db_query,
    db_execute_many,
    reconnect_database,
    set_user_context,
    clear_user_context,
)


# ─── Generic daily-count key manager ────────────────────────────────────────


class DailyKeyManager:
    """Reusable daily-count key rotation engine.

    Parameterized by table names so Gemini and OpenRouter can share
    the same SQL logic without code duplication.
    """

    def __init__(self, keys_table: str, usage_table: str):
        self.keys_table = keys_table
        self.usage_table = usage_table

    def _today(self) -> date:
        return datetime.now(get_pacific_tz()).date()

    async def get_available_key(
        self, model_name: str, conn=None
    ) -> Optional[Dict[str, Any]]:
        """Get the least-used key for the given model today."""
        today = self._today()
        query = f"""
            SELECT ak.key_hash, ak.api_key,
                   COALESCE(ku.request_count, 0) as request_count
            FROM {self.keys_table} ak
            LEFT JOIN {self.usage_table} ku ON ak.key_hash = ku.key_hash
                AND ku.model_name = $1 AND ku.usage_date = $2
            ORDER BY COALESCE(ku.request_count, 0) ASC
            LIMIT 1
        """
        results = await db_query(query, (model_name, today), conn=conn)
        if results:
            return {"key_hash": results[0]["key_hash"], "api_key": results[0]["api_key"]}
        return None

    async def increment_usage(self, key_hash: str, model_name: str):
        """UPSERT a +1 into the daily usage counter."""
        today = self._today()
        query = f"""
            INSERT INTO {self.usage_table}
                (key_hash, model_name, usage_date, request_count)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (key_hash, model_name, usage_date)
            DO UPDATE SET request_count = {self.usage_table}.request_count + 1
            RETURNING request_count;
        """
        return await db_query(query, (key_hash, model_name, today))

    async def is_key_available(
        self, key_hash: str, model_name: str, daily_limit: Optional[int], conn=None
    ) -> bool:
        """Check if a key is under its daily threshold."""
        if not daily_limit:
            return True
        today = self._today()
        query = f"""
            SELECT COALESCE(request_count, 0) as request_count
            FROM {self.usage_table}
            WHERE key_hash = $1 AND model_name = $2 AND usage_date = $3
        """
        result = await db_query(query, (key_hash, model_name, today), conn=conn)
        current_usage = result[0]["request_count"] if result else 0
        return current_usage < daily_limit * settings.LIMIT_THRESHOLD_PERCENT

    async def get_fresh_available_key(
        self, model_name: str, daily_limit: Optional[int], conn=None
    ) -> Optional[Dict[str, Any]]:
        """Find the least-used key that is still under the daily limit."""
        today = self._today()

        if not daily_limit:
            keys = await db_query(
                f"SELECT * FROM {self.keys_table} LIMIT 1", conn=conn
            )
            return keys[0] if keys else None

        query = f"""
            SELECT ak.key_hash, ak.api_key,
                   COALESCE(ku.request_count, 0) as request_count
            FROM {self.keys_table} ak
            LEFT JOIN {self.usage_table} ku ON ak.key_hash = ku.key_hash
                AND ku.model_name = $1 AND ku.usage_date = $2
            ORDER BY COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (model_name, today), conn=conn)
        if not results:
            return None

        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
        for row in results:
            if row["request_count"] < threshold:
                return {"key_hash": row["key_hash"], "api_key": row["api_key"]}
        return None


# Singletons
_gemini_km = DailyKeyManager("api_keys", "key_usage")
_openrouter_km = DailyKeyManager("openrouter_api_keys", "openrouter_key_usage")


# ─── Gemini key helpers (public API — signatures unchanged) ──────────────────


async def get_model_daily_limit(model_name: str) -> Optional[int]:
    async with db_manager._cache_lock:
        if (
            hasattr(db_manager, "_model_config_cache")
            and model_name in db_manager._model_config_cache
        ):
            return db_manager._model_config_cache[model_name]

    try:
        res = await db_query(
            "SELECT daily_limit FROM model_configuration WHERE model_name = $1",
            (model_name,),
        )
        limit = res[0]["daily_limit"] if res else None

        async with db_manager._cache_lock:
            if hasattr(db_manager, "_model_config_cache"):
                db_manager._model_config_cache[model_name] = limit
        return limit
    except Exception as e:
        logging.warning("Failed to fetch limit for %s: %s", model_name, e)
        return None


async def _is_key_available(key_hash: str, model_name: str, conn=None) -> bool:
    daily_limit = await get_model_daily_limit(model_name)
    return await _gemini_km.is_key_available(key_hash, model_name, daily_limit, conn=conn)


async def _get_fresh_available_key(
    model_name: str, conn=None
) -> Optional[Dict[str, Any]]:
    daily_limit = await get_model_daily_limit(model_name)
    return await _gemini_km.get_fresh_available_key(model_name, daily_limit, conn=conn)


async def invalidate_key_cache(model_name: str = None):
    async with db_manager._cache_lock:
        if model_name:
            if model_name in db_manager._active_keys_cache:
                del db_manager._active_keys_cache[model_name]
        else:
            db_manager._active_keys_cache.clear()


async def get_available_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    # Optimistic cache check (no DB lock needed)
    cached_key = None
    async with db_manager._cache_lock:
        if model_name in db_manager._active_keys_cache:
            cached_key = db_manager._active_keys_cache[model_name]

    if cached_key:
        return cached_key

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            new_key = await _get_fresh_available_key(model_name, conn=conn)

            if new_key:
                async with db_manager._cache_lock:
                    db_manager._active_keys_cache[model_name] = new_key

            return new_key
        finally:
            await clear_user_context(conn=conn)


async def get_current_active_gemini_key(model_name: str) -> Optional[Dict[str, Any]]:
    today_pacific = _gemini_km._today()
    daily_limit = await get_model_daily_limit(model_name)

    if not daily_limit:
        keys = await db_query("SELECT * FROM api_keys LIMIT 1")
        return keys[0] if keys else None

    threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
    active_key_query = """
        SELECT ak.key_hash, ak.api_key, COALESCE(ku.request_count, 0) as request_count
        FROM api_keys ak
        LEFT JOIN key_usage ku ON ak.key_hash = ku.key_hash 
            AND ku.model_name = $1 AND ku.usage_date = $2
        WHERE COALESCE(ku.request_count, 0) < $3
        ORDER BY COALESCE(ku.request_count, 0) ASC
        LIMIT 1
    """
    results = await db_query(active_key_query, (model_name, today_pacific, threshold))

    if results:
        return {"key_hash": results[0]["key_hash"], "api_key": results[0]["api_key"]}
    return None


async def increment_gemini_key_usage(key_hash: str, model_name: str):
    result = await _gemini_km.increment_usage(key_hash, model_name)
    current_usage = result[0]["request_count"] if result else 0

    daily_limit = await get_model_daily_limit(model_name)
    if daily_limit:
        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT

        if current_usage >= threshold:
            await invalidate_key_cache(model_name)
        else:
            async with db_manager._cache_lock:
                if model_name in db_manager._cache_last_updated:
                    db_manager._cache_last_updated[model_name] = time.time()


# ─── Generic monthly-credit key manager ─────────────────────────────────────


class MonthlyKeyManager:
    """Reusable monthly-credit key rotation engine.

    Parameterized by table names so any monthly-credit provider can share
    the same SQL logic without code duplication.
    """

    def __init__(
        self,
        keys_table: str,
        usage_table: str,
        credit_limit: float,
        threshold_percent: float,
    ):
        self.keys_table = keys_table
        self.usage_table = usage_table
        self.credit_limit = credit_limit
        self.threshold_percent = threshold_percent

    def _current_month(self) -> str:
        return datetime.now(UTC_TZ).strftime("%Y-%m")

    async def get_available_key(self) -> Optional[Dict[str, Any]]:
        """Get the least-used key that's still under the monthly credit threshold."""
        current_month = self._current_month()
        query = f"""
            SELECT ak.key_hash, ak.api_key,
                   COALESCE(ku.credit_usage, 0) as credit_usage
            FROM {self.keys_table} ak
            LEFT JOIN {self.usage_table} ku ON ak.key_hash = ku.key_hash
                AND ku.usage_month = $1
            ORDER BY COALESCE(ku.credit_usage, 0) ASC
        """
        results = await db_query(query, (current_month,))
        threshold = self.credit_limit * self.threshold_percent

        for row in results:
            if row["credit_usage"] < threshold:
                return {"key_hash": row["key_hash"], "api_key": row["api_key"]}
        return None

    async def increment_usage(self, key_hash: str, cost: int):
        """UPSERT a +cost into the monthly credit counter."""
        current_month = self._current_month()
        query = f"""
            INSERT INTO {self.usage_table}
                (key_hash, usage_month, credit_usage)
            VALUES ($1, $2, $3)
            ON CONFLICT (key_hash, usage_month)
            DO UPDATE SET credit_usage = {self.usage_table}.credit_usage + $4;
        """
        await db_query(query, (key_hash, current_month, cost, cost))


# Singleton
_tavily_km = MonthlyKeyManager(
    keys_table="tavily_api_keys",
    usage_table="tavily_key_usage",
    credit_limit=settings.TAVILY_MONTHLY_CREDIT_LIMIT,
    threshold_percent=settings.TAVILY_LIMIT_THRESHOLD_PERCENT,
)


async def get_available_tavily_key():
    return await _tavily_km.get_available_key()


async def increment_tavily_key_usage(key_hash: str, cost: int):
    await _tavily_km.increment_usage(key_hash, cost)


async def force_update_tavily_keys():
    try:
        from app.config import get_settings

        settings_obj = get_settings()
        if not settings_obj or not settings_obj.TAVILY_API_KEYS:
            return False
        await db_query("DELETE FROM tavily_api_keys")
        keys_data = []
        for key in settings_obj.TAVILY_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            keys_data.append((key_hash, key))

        if keys_data:
            await db_execute_many(
                "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)",
                keys_data,
            )
        await db_query("DELETE FROM tavily_key_usage")
        async with db_manager._cache_lock:
            db_manager._active_keys_cache.clear()
        return True
    except Exception:
        return False


# ─── OpenRouter key helpers (delegates to DailyKeyManager) ───────────────────


async def get_available_openrouter_key(model_name: str) -> Optional[Dict[str, Any]]:
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            return await _openrouter_km.get_available_key(model_name, conn=conn)
        finally:
            await clear_user_context(conn=conn)


async def increment_openrouter_key_usage(key_hash: str, model_name: str):
    await _openrouter_km.increment_usage(key_hash, model_name)

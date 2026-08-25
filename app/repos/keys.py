"""
API key management for Gemini, OpenRouter, and Tavily providers.

Handles key rotation, usage tracking, cache invalidation,
and daily/monthly limit enforcement.

Extracted from app/database.py to isolate key-management domain logic.
"""

import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg

from app.config import UTC_TZ, settings
from app.crypto import encrypt_api_key, safe_decrypt
from app.database import (
    clear_user_context,
    db_execute_many,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.utils.time import get_pacific_tz

_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


# ─── Generic daily-count key manager ────────────────────────────────────────


class DailyKeyManager:
    """Reusable daily-count key rotation engine.

    Parameterized by table names so Gemini and OpenRouter can share
    the same SQL logic without code duplication.
    """

    def __init__(self, keys_table: str, usage_table: str):
        if not _SAFE_TABLE_RE.match(keys_table):
            raise ValueError(f"Unsafe table name: {keys_table!r}")
        if not _SAFE_TABLE_RE.match(usage_table):
            raise ValueError(f"Unsafe table name: {usage_table!r}")
        self.keys_table = keys_table
        self.usage_table = usage_table

    def _today(self) -> date:
        return datetime.now(get_pacific_tz()).date()

    async def get_available_key(self, model_name: str, conn=None) -> dict[str, Any] | None:
        """Get the least-used key for the given model today."""
        today = self._today()
        query = f"""
            SELECT ak.key_hash, ak.api_key,
                   COALESCE(ku.request_count, 0) as request_count
            FROM {self.keys_table} ak
            LEFT JOIN {self.usage_table} ku ON ak.key_hash = ku.key_hash
                AND ku.model_name = $1 AND ku.usage_date = $2
            ORDER BY COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (model_name, today), conn=conn)
        for row in results:
            try:
                return {
                    "key_hash": row["key_hash"],
                    "api_key": safe_decrypt(row["api_key"]),
                }
            except Exception as e:
                logging.error("Failed to decrypt key %s: %s", row["key_hash"][:8], e)
                continue
        return None

    async def increment_usage(self, key_hash: str, model_name: str) -> list[dict[str, Any]]:
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
        result = await db_query(query, (key_hash, model_name, today))
        # Structured observability: log threshold approach
        if result:
            count = result[0]["request_count"]
            if count == 1:
                logging.info(
                    "KEY_EVENT key_first_use key=%s… model=%s provider=%s",
                    key_hash[:8],
                    model_name,
                    self.keys_table,
                )
            elif count % 100 == 0:
                logging.info(
                    "KEY_EVENT key_usage_milestone key=%s… model=%s count=%d provider=%s",
                    key_hash[:8],
                    model_name,
                    count,
                    self.keys_table,
                )
        return result

    async def is_key_available(self, key_hash: str, model_name: str, daily_limit: int | None, conn=None) -> bool:
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
        self,
        model_name: str,
        daily_limit: int | None,
        excluded_hashes: set[str] | None = None,
        conn=None,
    ) -> dict[str, Any] | None:
        """Find the least-used key that is still under the daily limit.

        Two-tier selection:
            Tier 1 — active keys (status='active' or no status row)
            Tier 2 — suspended keys whose cooldown has expired

        Keys in *excluded_hashes* are filtered at the SQL level.
        """
        today = self._today()
        excluded = list(excluded_hashes) if excluded_hashes else []

        if not daily_limit:
            query = f"""
                SELECT ak.key_hash, ak.api_key
                FROM {self.keys_table} ak
                LEFT JOIN key_model_status kms
                    ON ak.key_hash = kms.key_hash AND kms.model_name = $1
                WHERE ak.key_hash != ALL($2)
                  AND (
                      COALESCE(kms.status, 'active') = 'active'
                      OR kms.suspended_until < NOW()
                  )
                ORDER BY
                    CASE WHEN COALESCE(kms.status, 'active') = 'active' THEN 0 ELSE 1 END
            """
            keys = await db_query(query, (model_name, excluded), conn=conn)
            for row in keys:
                try:
                    return {
                        "key_hash": row["key_hash"],
                        "api_key": safe_decrypt(row["api_key"]),
                    }
                except Exception as e:
                    logging.error("Failed to decrypt key %s: %s", row["key_hash"][:8], e)
                    continue
            return None

        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
        query = f"""
            SELECT ak.key_hash, ak.api_key,
                   COALESCE(ku.request_count, 0) AS request_count,
                   COALESCE(kms.status, 'active') AS key_status
            FROM {self.keys_table} ak
            LEFT JOIN {self.usage_table} ku
                ON ak.key_hash = ku.key_hash
                AND ku.model_name = $1 AND ku.usage_date = $2
            LEFT JOIN key_model_status kms
                ON ak.key_hash = kms.key_hash AND kms.model_name = $1
            WHERE ak.key_hash != ALL($3)
              AND (
                  COALESCE(kms.status, 'active') = 'active'
                  OR kms.suspended_until < NOW()
              )
            ORDER BY
                CASE WHEN COALESCE(kms.status, 'active') = 'active' THEN 0 ELSE 1 END,
                COALESCE(ku.request_count, 0) ASC
        """
        results = await db_query(query, (model_name, today, excluded), conn=conn)
        if not results:
            return None

        for row in results:
            if row["request_count"] < threshold:
                try:
                    return {
                        "key_hash": row["key_hash"],
                        "api_key": safe_decrypt(row["api_key"]),
                    }
                except Exception as e:
                    logging.error("Failed to decrypt key %s: %s", row["key_hash"][:8], e)
                    continue
        return None


# Singletons
_gemini_km = DailyKeyManager("api_keys", "key_usage")
_openrouter_km = DailyKeyManager("openrouter_api_keys", "openrouter_key_usage")


# ─── Gemini key helpers (public API — signatures unchanged) ──────────────────


async def get_model_daily_limit(model_name: str) -> int | None:
    if model_name in db_manager._model_config_cache:
        return db_manager._model_config_cache[model_name]

    try:
        res = await db_query(
            "SELECT daily_limit FROM public.model_configuration WHERE model_name = $1",
            (model_name,),
        )
        limit = res[0]["daily_limit"] if res else None

        # Fallback to settings.DAILY_LIMITS when DB has no entry for this model
        if limit is None:
            limit = settings.DAILY_LIMITS.get(model_name)

        db_manager._model_config_cache[model_name] = limit
        return limit
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to fetch limit for %s: %s", model_name, e)
        # Fallback to config even on DB error
        return settings.DAILY_LIMITS.get(model_name)


async def _is_key_available(key_hash: str, model_name: str, conn=None) -> bool:
    daily_limit = await get_model_daily_limit(model_name)
    return await _gemini_km.is_key_available(key_hash, model_name, daily_limit, conn=conn)


async def _get_fresh_available_key(
    model_name: str,
    excluded_hashes: set[str] | None = None,
    conn=None,
) -> dict[str, Any] | None:
    daily_limit = await get_model_daily_limit(model_name)
    return await _gemini_km.get_fresh_available_key(
        model_name,
        daily_limit,
        excluded_hashes=excluded_hashes,
        conn=conn,
    )


async def invalidate_key_cache(model_name: str | None = None) -> None:
    if model_name:
        if model_name in db_manager._active_keys_cache:
            del db_manager._active_keys_cache[model_name]
    else:
        db_manager._active_keys_cache.clear()


async def get_available_gemini_key(
    model_name: str,
    excluded_hashes: set[str] | None = None,
) -> dict[str, Any] | None:
    # When exclusions are requested, skip cache (caller wants a *different* key)
    if not excluded_hashes:
        cached_key = None
        if model_name in db_manager._active_keys_cache:
            cached_key = db_manager._active_keys_cache[model_name]
        if cached_key:
            return cached_key

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            new_key = await _get_fresh_available_key(
                model_name,
                excluded_hashes=excluded_hashes,
                conn=conn,
            )

            if new_key and not excluded_hashes:
                db_manager._active_keys_cache[model_name] = new_key

            return new_key
        finally:
            await clear_user_context(conn=conn)


async def get_current_active_gemini_key(model_name: str) -> dict[str, Any] | None:
    """Get the currently active Gemini API key with the lowest usage.

    Delegates to DailyKeyManager.get_fresh_available_key to avoid duplicating
    the key selection logic.
    """
    daily_limit = await get_model_daily_limit(model_name)
    return await _gemini_km.get_fresh_available_key(model_name, daily_limit)


async def count_gemini_keys() -> int:
    """Return the total number of Gemini API keys currently in the pool.

    Used to dynamically size max_key_retries in batch generation tasks so
    the router can spread load across the full pool instead of a hardcoded cap.
    """
    rows = await db_query("SELECT COUNT(*) AS cnt FROM api_keys", ())
    return int(rows[0]["cnt"]) if rows else 0


async def increment_gemini_key_usage(key_hash: str, model_name: str) -> None:
    result = await _gemini_km.increment_usage(key_hash, model_name)
    current_usage = result[0]["request_count"] if result else 0

    daily_limit = await get_model_daily_limit(model_name)
    if daily_limit:
        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT
        usage_pct = (current_usage / daily_limit) * 100

        if current_usage >= threshold:
            logging.warning(
                "KEY_EVENT key_threshold_reached key=%s… model=%s usage=%d/%d (%.0f%%) — rotating",
                key_hash[:8],
                model_name,
                current_usage,
                daily_limit,
                usage_pct,
            )
            await invalidate_key_cache(model_name)
        elif usage_pct >= 70:
            logging.info(
                "KEY_EVENT key_nearing_limit key=%s… model=%s usage=%d/%d (%.0f%%)",
                key_hash[:8],
                model_name,
                current_usage,
                daily_limit,
                usage_pct,
            )


# ─── KeyStatusManager (per-model key health, DB-backed) ─────────────────────


# Cooldown durations per error category
_PENALTY_DURATIONS: dict[str, timedelta] = {
    "permanent": timedelta(hours=24),
    "rate_limit": timedelta(seconds=15),
    "transient": timedelta(seconds=15),
    # "quota" is handled specially (until midnight PT)
}
_MAX_SUSPENSION = timedelta(days=7)


def _compute_suspended_until(
    category: str,
    failure_count: int,
) -> datetime:
    """Return the UTC timestamp until which the key should be suspended."""
    now = datetime.now(UTC_TZ)

    if category == "quota":
        # Suspend until midnight Pacific time
        pacific = get_pacific_tz()
        pacific_now = datetime.now(pacific)
        next_midnight_pt = pacific_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        return next_midnight_pt.astimezone(UTC_TZ)

    base = _PENALTY_DURATIONS.get(category, timedelta(seconds=15))

    # Exponential backoff on repeated failures (capped)
    multiplier = min(2 ** (failure_count - 1), 128) if failure_count > 1 else 1
    cooldown = min(base * multiplier, _MAX_SUSPENSION)
    return now + cooldown


class KeyStatusManager:
    """DB-backed per-model key health tracker.

    Replaces the in-memory KeyHealth dataclass with persistent state.
    Write-through on status changes; cached reads via _active_keys_cache TTL.
    """

    def __init__(self):
        self._suspension_cache: dict[str, datetime] = {}

    async def suspend_key(
        self,
        key_hash: str,
        model_name: str,
        error_category: str,
        error_text: str = "",
    ) -> None:
        """Suspend a key for a specific model with category-aware cooldown."""
        cache_key = f"{key_hash}:{model_name}"
        now_utc = datetime.now(UTC_TZ)

        # Drop duplicate suspension requests within 5 seconds for the same key.
        # Prevents a "thundering herd" of concurrent DB writes when many Live
        # WS sessions hit the same quota-exhausted key simultaneously.
        # NOTE: No asyncio.Lock is needed here. The check + write below has no
        # `await` between them, so asyncio's cooperative scheduler cannot switch
        # coroutines mid-way. The entire block executes atomically.
        cached_at = self._suspension_cache.get(cache_key)
        if cached_at is not None and (now_utc - cached_at).total_seconds() < 5:
            return
        self._suspension_cache[cache_key] = now_utc

        # Read current failure_count to compute backoff
        rows = await db_query(
            "SELECT failure_count FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
            (key_hash, model_name),
        )
        prev_failures = rows[0]["failure_count"] if rows else 0
        new_failures = prev_failures + 1
        suspended_until = _compute_suspended_until(error_category, new_failures)

        await db_query(
            """
            INSERT INTO key_model_status
                (key_hash, model_name, status, suspended_until,
                 failure_count, last_error, updated_at)
            VALUES ($1, $2, 'suspended', $3, $4, $5, NOW())
            ON CONFLICT (key_hash, model_name)
            DO UPDATE SET
                status = 'suspended',
                suspended_until = $3,
                failure_count = $4,
                last_error = $5,
                updated_at = NOW()
            """,
            (key_hash, model_name, suspended_until, new_failures, error_text[:500]),
        )

        await invalidate_key_cache(model_name)

        logging.warning(
            "Key %s… suspended for model %s until %s (category=%s, failures=%d)",
            key_hash[:8],
            model_name,
            suspended_until.isoformat(),
            error_category,
            new_failures,
        )

    async def record_success(
        self,
        key_hash: str,
        model_name: str,
    ) -> None:
        """Reset key to active after a successful request."""
        await db_query(
            """
            INSERT INTO key_model_status
                (key_hash, model_name, status, suspended_until,
                 failure_count, last_error, updated_at)
            VALUES ($1, $2, 'active', NULL, 0, NULL, NOW())
            ON CONFLICT (key_hash, model_name)
            DO UPDATE SET
                status = 'active',
                suspended_until = NULL,
                failure_count = 0,
                last_error = NULL,
                updated_at = NOW()
            """,
            (key_hash, model_name),
        )

    async def get_all_statuses(self, conn=None) -> list[dict[str, Any]]:
        """Return all key statuses for diagnostics / dashboard."""
        return await db_query(
            "SELECT key_hash, model_name, status, suspended_until, "
            "failure_count, last_error, updated_at "
            "FROM key_model_status ORDER BY updated_at DESC",
            conn=conn,
        )

    async def get_health_summary(self, conn=None) -> dict[str, Any]:
        """Return a summary of key health for observability dashboard."""
        statuses = await self.get_all_statuses(conn=conn)
        active = sum(1 for s in statuses if s["status"] == "active")
        suspended = sum(1 for s in statuses if s["status"] == "suspended")
        total_failures = sum(s.get("failure_count", 0) for s in statuses)
        return {
            "total_keys_tracked": len(statuses),
            "active": active,
            "suspended": suspended,
            "total_failures": total_failures,
            "keys": [
                {
                    "key": s["key_hash"][:8] + "…",
                    "model": s["model_name"],
                    "status": s["status"],
                    "failures": s.get("failure_count", 0),
                    "suspended_until": s["suspended_until"].isoformat() if s.get("suspended_until") else None,
                    "last_error": (s.get("last_error") or "")[:100],
                }
                for s in statuses[:20]  # Cap at 20 for dashboard
            ],
        }


# Singleton
_key_status_manager: KeyStatusManager | None = None


def get_key_status_manager() -> KeyStatusManager:
    """Get the singleton KeyStatusManager instance."""
    global _key_status_manager
    if _key_status_manager is None:
        _key_status_manager = KeyStatusManager()
    return _key_status_manager


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
        if not _SAFE_TABLE_RE.match(keys_table):
            raise ValueError(f"Unsafe table name: {keys_table!r}")
        if not _SAFE_TABLE_RE.match(usage_table):
            raise ValueError(f"Unsafe table name: {usage_table!r}")
        self.keys_table = keys_table
        self.usage_table = usage_table
        self.credit_limit = credit_limit
        self.threshold_percent = threshold_percent

    def _current_month(self) -> str:
        return datetime.now(UTC_TZ).strftime("%Y-%m")

    async def get_available_key(self) -> dict[str, Any] | None:
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
                try:
                    return {
                        "key_hash": row["key_hash"],
                        "api_key": safe_decrypt(row["api_key"]),
                    }
                except Exception as e:
                    logging.error("Failed to decrypt key %s: %s", row["key_hash"][:8], e)
                    continue
        return None

    async def increment_usage(self, key_hash: str, cost: int) -> None:
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
    credit_limit=settings.TAVILY_MONTHLY_CREDIT_LIMIT if settings else 1000,
    threshold_percent=settings.TAVILY_LIMIT_THRESHOLD_PERCENT if settings else 80,
)


async def get_available_tavily_key() -> dict[str, Any] | None:
    return await _tavily_km.get_available_key()


async def increment_tavily_key_usage(key_hash: str, cost: int) -> None:
    await _tavily_km.increment_usage(key_hash, cost)


async def force_update_tavily_keys() -> bool:
    try:
        from app.config import get_settings

        settings_obj = get_settings()
        if not settings_obj or not settings_obj.TAVILY_API_KEYS:
            return False
        await db_query("DELETE FROM tavily_api_keys")
        keys_data = []
        for key in settings_obj.TAVILY_API_KEYS:
            key_hash = hashlib.sha256(key.encode()).hexdigest()
            keys_data.append((key_hash, encrypt_api_key(key)))

        if keys_data:
            await db_execute_many(
                "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)",
                keys_data,
            )
        await db_query("DELETE FROM tavily_key_usage")
        db_manager._active_keys_cache.clear()
        return True
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


# ─── OpenRouter key helpers (delegates to DailyKeyManager) ───────────────────


async def get_available_openrouter_key(
    model_name: str,
    excluded_hashes: set[str] | None = None,
) -> dict[str, Any] | None:
    if not db_manager.is_connected:
        await reconnect_database()

    daily_limit = await get_model_daily_limit(model_name)
    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(settings.ADMIN_ID, True, conn=conn)
        try:
            return await _openrouter_km.get_fresh_available_key(
                model_name,
                daily_limit,
                excluded_hashes=excluded_hashes,
                conn=conn,
            )
        finally:
            await clear_user_context(conn=conn)


async def increment_openrouter_key_usage(key_hash: str, model_name: str) -> None:
    await _openrouter_km.increment_usage(key_hash, model_name)

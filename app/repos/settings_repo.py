"""
Repository for bot-wide runtime configuration stored in global_settings table.

Provides a simple key-value interface with a short-lived TTL cache so that
reads are O(1) in-memory for the common case while still picking up changes
set by admins within ~30 seconds.

Self-healing: if the table is missing (migrations haven't run yet), the first
access lazily creates it so the feature never blocks bot startup.
"""

import logging

import asyncpg
from cachetools import TTLCache

from app import database as db

logger = logging.getLogger(__name__)

# TTL cache: up to 64 distinct keys, each valid for 30 seconds.
# This means admin changes propagate to all workers within ~30 s
# without any per-request DB round-trip in steady state.
_cache: TTLCache = TTLCache(maxsize=64, ttl=30)

# Singleton flag — once the table is confirmed (or created), skip re-checks.
_table_verified: bool = False


async def _ensure_table() -> None:
    """Create global_settings table if it doesn't exist (lazy bootstrap)."""
    global _table_verified
    if _table_verified:
        return
    try:
        await db.db_query(
            """
            CREATE TABLE IF NOT EXISTS global_settings (
                key_name   TEXT        PRIMARY KEY,
                value_data TEXT        NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.db_query("ALTER TABLE global_settings ENABLE ROW LEVEL SECURITY")
        await db.db_query(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = current_schema()
                      AND tablename = 'global_settings'
                      AND policyname = 'global_settings_policy'
                ) THEN
                    CREATE POLICY global_settings_policy ON global_settings
                    FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
                END IF;
            END
            $$
            """
        )
        _table_verified = True
        logger.info("settings_repo: global_settings table verified/created")
    except Exception as exc:
        logger.warning("settings_repo: failed to bootstrap global_settings: %s", exc)
        raise


async def get_global_setting(key: str, default: str = "") -> str:
    """Return the value for *key* from global_settings, or *default*.

    Results are cached for 30 seconds.  Errors are logged and *default*
    is returned so callers are never blocked by a transient DB issue.
    """
    if key in _cache:
        return _cache[key]  # type: ignore[return-value]

    try:
        rows = await db.db_query(
            "SELECT value_data FROM global_settings WHERE key_name = $1",
            (key,),
        )
        value: str = rows[0]["value_data"] if rows else default
    except asyncpg.UndefinedTableError:
        # Table doesn't exist yet — bootstrap it, then retry once.
        logger.warning("settings_repo: table missing, bootstrapping…")
        await _ensure_table()
        try:
            rows = await db.db_query(
                "SELECT value_data FROM global_settings WHERE key_name = $1",
                (key,),
            )
            value = rows[0]["value_data"] if rows else default
        except Exception as exc:
            logger.warning(
                "settings_repo: retry after bootstrap failed for '%s': %s — using default", key, exc
            )
            value = default
    except Exception as exc:
        logger.warning("settings_repo: failed to read '%s': %s — using default", key, exc)
        value = default

    _cache[key] = value
    return value


async def set_global_setting(key: str, value: str) -> None:
    """Upsert *key* → *value* in global_settings and invalidate the local cache.

    The UPSERT ensures the row is created on first write even if the INSERT
    from the migration seed was skipped or rolled back.
    """
    await _ensure_table()
    await db.db_query(
        """
        INSERT INTO global_settings (key_name, value_data, updated_at)
        VALUES ($1, $2, CURRENT_TIMESTAMP)
        ON CONFLICT (key_name) DO UPDATE
            SET value_data = EXCLUDED.value_data,
                updated_at  = EXCLUDED.updated_at
        """,
        (key, value),
    )
    # Immediately evict so the next read fetches the fresh value.
    _cache.pop(key, None)
    logger.info("settings_repo: updated '%s'", key)


async def delete_global_setting(key: str) -> None:
    """Delete *key* and immediately invalidate its cached value."""
    await _ensure_table()
    await db.db_query(
        "DELETE FROM global_settings WHERE key_name = $1",
        (key,),
    )
    _cache.pop(key, None)
    logger.info("settings_repo: deleted '%s'", key)


def invalidate(key: str | None = None) -> None:
    """Evict *key* from the in-process cache (or clear all if None).

    Useful in tests or when the DB is written from outside this process.
    """
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)

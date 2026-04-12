"""
Repository for bot-wide runtime configuration stored in global_settings table.

Provides a simple key-value interface with a short-lived TTL cache so that
reads are O(1) in-memory for the common case while still picking up changes
set by admins within ~30 seconds.
"""

import logging
from typing import Optional

from cachetools import TTLCache

from app import database as db

logger = logging.getLogger(__name__)

# TTL cache: up to 64 distinct keys, each valid for 30 seconds.
# This means admin changes propagate to all workers within ~30 s
# without any per-request DB round-trip in steady state.
_cache: TTLCache = TTLCache(maxsize=64, ttl=30)


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
    except Exception as exc:
        logger.warning("settings_repo: failed to read '%s': %s — using default '%s'", key, exc, default)
        value = default

    _cache[key] = value
    return value


async def set_global_setting(key: str, value: str) -> None:
    """Upsert *key* → *value* in global_settings and invalidate the local cache.

    The UPSERT ensures the row is created on first write even if the INSERT
    from the migration seed was skipped or rolled back.
    """
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
    logger.info("settings_repo: set '%s' = '%s'", key, value)


def invalidate(key: Optional[str] = None) -> None:
    """Evict *key* from the in-process cache (or clear all if None).

    Useful in tests or when the DB is written from outside this process.
    """
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)

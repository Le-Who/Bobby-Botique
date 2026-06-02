"""Cross-process Telegram webhook update dedupe."""

from __future__ import annotations

import asyncio
import logging
import time

try:
    from app.cache import redis_client
except Exception:  # pragma: no cover - defensive fallback during partial imports
    redis_client = None

logger = logging.getLogger(__name__)

_DEDUP_KEY_PREFIX = "telegram:webhook:update:"


async def should_accept_webhook_update(
    update_id: int,
    seen_update_ids: dict[int, float],
    seen_lock: asyncio.Lock,
    *,
    ttl_seconds: float = 180.0,
    capacity: int = 10_000,
) -> bool:
    """Return False when a Telegram update was already claimed.

    Redis is the primary guard so overlapping old/new bot containers cannot both
    process the same webhook update during deploys. The in-process map remains a
    fallback for local/dev or degraded Redis.
    """
    ttl = max(1, int(ttl_seconds))
    if redis_client:
        try:
            claimed = await redis_client.set(f"{_DEDUP_KEY_PREFIX}{update_id}", "1", ex=ttl, nx=True)
            return bool(claimed)
        except Exception as exc:
            logger.warning("Webhook Redis dedupe unavailable; using local fallback: %s", exc)

    now = time.monotonic()
    async with seen_lock:
        stale = [uid for uid, ts in seen_update_ids.items() if now - ts > ttl_seconds]
        for uid in stale:
            seen_update_ids.pop(uid, None)

        if update_id in seen_update_ids:
            return False

        if len(seen_update_ids) >= capacity:
            oldest = sorted(seen_update_ids.items(), key=lambda item: item[1])[: max(1, capacity // 10)]
            for uid, _ in oldest:
                seen_update_ids.pop(uid, None)
        seen_update_ids[update_id] = now
        return True

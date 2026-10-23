"""Rolling Gemini TTS minute admission; sent requests retain their usage.

Redis owns the window across replicas. Without Redis, the event loop owns a
local window. An uncertain Redis write must never open a second local budget.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Any

_RESERVE_MINUTE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return {0, math.min(60000, math.max(1, math.ceil((tonumber(oldest[2]) + 60 - now) * 1000)))}
end
local pending = tonumber(ARGV[3])
redis.call('ZADD', KEYS[1], now + pending, ARGV[2])
local ttl = math.ceil(pending + 61)
if redis.call('TTL', KEYS[1]) < ttl then redis.call('EXPIRE', KEYS[1], ttl) end
return {1, 0}
"""

_COMMIT_MINUTE = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZADD', KEYS[1], 'XX', now, ARGV[1])
return 1
"""


class TTSMinuteQuota:
    def __init__(self) -> None:
        self._local: dict[str, dict[str, float]] = {}

    @staticmethod
    def _key(scope: str) -> str:
        return "gemaibotv2:tts:rpm:" + hashlib.sha256(scope.encode()).hexdigest()

    async def reserve(self, scope: str, limit: int, *, pending_for: float = 0) -> tuple[tuple[str, Any] | None, float]:
        from app.cache import redis_client

        token = uuid.uuid4().hex
        if redis_client is not None:
            admitted, retry_ms = await redis_client.eval(
                _RESERVE_MINUTE, 1, self._key(scope), limit, token, pending_for
            )
            return ((token, redis_client), 0.0) if admitted else (None, float(retry_ms) / 1000)

        now = time.monotonic()
        # Prune idle scopes too: retired keys/models must not accumulate forever.
        for key, entries in tuple(self._local.items()):
            live = {item: expires for item, expires in entries.items() if expires > now}
            if live:
                self._local[key] = live
            else:
                del self._local[key]
        entries = self._local.setdefault(scope, {})
        if len(entries) >= limit:
            return None, min(60.0, min(entries.values()) - now)
        # Hold admission through the request deadline while RPD is pending.
        # commit() starts the actual usage window immediately before dispatch.
        entries[token] = now + pending_for + 60.0
        return (token, None), 0.0

    async def commit(self, scope: str, reservation: tuple[str, Any]) -> None:
        token, redis = reservation
        if redis is None:
            entries = self._local.get(scope, {})
            if token not in entries:
                raise RuntimeError("TTS minute reservation expired before dispatch")
            entries[token] = time.monotonic() + 60.0
        elif not await redis.eval(_COMMIT_MINUTE, 1, self._key(scope), token):
            raise RuntimeError("TTS minute reservation expired before dispatch")

    async def release(self, scope: str, reservation: tuple[str, Any]) -> None:
        """Refund only a request that never reached provider dispatch."""
        token, redis = reservation
        if redis is None:
            self._local.get(scope, {}).pop(token, None)
        else:
            await redis.zrem(self._key(scope), token)

    async def release_unsent(self, scope: str, reservation: tuple[str, Any]) -> None:
        async def release_bounded() -> None:
            try:
                async with asyncio.timeout(5.0):
                    await self.release(scope, reservation)
            except Exception as error:
                # Redis will expire an uncertain token; do not replace a local
                # admission refusal with a provider/key-health failure.
                logging.warning("TTS minute refund unavailable type=%s", type(error).__name__)

        # Always await the owned cleanup, including repeated cancellation.
        cleanup = asyncio.create_task(release_bounded())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


tts_minute_quota = TTSMinuteQuota()

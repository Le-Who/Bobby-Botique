"""Per-key/model sliding minute admission shared by Gemini workloads."""

import hashlib
import time

from app.providers.tts_quota import TTSMinuteQuota

_AVAILABLE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60)
return redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[1]) and 1 or 0
"""


class GeminiMinuteQuota(TTSMinuteQuota):
    """Reuse the proven reservation lifecycle with a separate quota namespace."""

    @staticmethod
    def _key(scope: str) -> str:
        return "gemaibotv2:gemini:rpm:" + hashlib.sha256(scope.encode()).hexdigest()

    async def available(self, scope: str, limit: int) -> bool:
        from app.cache import redis_client

        if redis_client is not None:
            return bool(await redis_client.eval(_AVAILABLE, 1, self._key(scope), limit))
        now = time.monotonic()
        return sum(expires > now for expires in self._local.get(scope, {}).values()) < limit


gemini_minute_quota = GeminiMinuteQuota()

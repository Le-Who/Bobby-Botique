"""Execute production TTS admission Lua against an explicitly disposable Redis."""

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from app.providers.tts_quota import TTSMinuteQuota

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_two_workers_share_atomic_minute_capacity_and_owned_refunds(monkeypatch):
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL must identify an explicitly isolated Redis")
    client = Redis.from_url(url, decode_responses=True)
    scope = "tts-contract:" + uuid.uuid4().hex
    first, second = TTSMinuteQuota(), TTSMinuteQuota()
    monkeypatch.setattr("app.cache.redis_client", client)
    try:
        results = await asyncio.gather(*((first if i % 2 else second).reserve(scope, 3) for i in range(20)))
        admitted = [token for token, _ in results if token is not None]
        assert len(admitted) == 3
        assert all(0 < delay <= 60 for token, delay in results if token is None)
        assert 0 < await client.ttl(first._key(scope)) <= 61
        await first.commit(scope, admitted[1])
        assert await client.zcard(first._key(scope)) == 3
        await first.release_unsent(scope, admitted[0])
        await first.release_unsent(scope, admitted[0])
        assert (await second.reserve(scope, 3))[0] is not None
        assert (await first.reserve(scope, 3))[0] is None
    finally:
        await client.delete(first._key(scope))
        await client.aclose()

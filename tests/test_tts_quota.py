"""Minute admission is a rolling usage window, not an in-flight semaphore."""

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def quota(monkeypatch):
    from app.providers import tts_quota

    now = [100.0]
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(tts_quota.time, "monotonic", lambda: now[0])
    return tts_quota.TTSMinuteQuota(), now


@pytest.mark.asyncio
async def test_concurrent_admission_cannot_exceed_minute_budget(quota):
    limiter, _ = quota
    results = await asyncio.gather(*(limiter.reserve("key-model", 3) for _ in range(20)))
    assert sum(token is not None for token, _ in results) == 3
    assert [delay for token, delay in results if token is None] == [60.0] * 17


@pytest.mark.asyncio
async def test_sent_requests_and_pending_reservations_share_the_same_three_slots(quota):
    limiter, now = quota
    sent, _ = await limiter.reserve("key-model", 3, pending_for=120)
    await limiter.commit("key-model", sent)
    pending, _ = await limiter.reserve("key-model", 3, pending_for=120)
    last_free, _ = await limiter.reserve("key-model", 3, pending_for=120)
    assert pending is not None and last_free is not None
    assert await limiter.reserve("key-model", 3) == (None, 60.0)

    # Only the sent request expires after its dispatch window. Both pending
    # requests keep their reservations until dispatch or owned cancellation.
    now[0] = 160.0
    assert (await limiter.reserve("key-model", 3))[0] is not None
    assert await limiter.reserve("key-model", 3) == (None, 60.0)


@pytest.mark.asyncio
async def test_minute_usage_expires_and_is_independent_for_each_key_and_model(quota):
    limiter, now = quota
    assert (await limiter.reserve("key-a/model-a", 1))[0] is not None
    assert (await limiter.reserve("key-b/model-a", 1))[0] is not None
    assert (await limiter.reserve("key-a/model-b", 1))[0] is not None
    now[0] = 159.0
    assert await limiter.reserve("key-a/model-a", 1) == (None, 1.0)
    now[0] = 160.0
    assert (await limiter.reserve("key-a/model-a", 1))[0] is not None


@pytest.mark.asyncio
async def test_unsent_reservation_releases_only_its_own_minute_slot(quota):
    limiter, _ = quota
    first, _ = await limiter.reserve("scope", 2)
    second, _ = await limiter.reserve("scope", 2)
    await limiter.release("scope", first)
    await limiter.release("scope", first)
    third, _ = await limiter.reserve("scope", 2)
    assert second is not None and third is not None
    assert await limiter.reserve("scope", 2) == (None, 60.0)


@pytest.mark.asyncio
async def test_redis_uncertain_write_never_falls_back_to_another_budget(quota, monkeypatch):
    limiter, _ = quota
    redis = type("Redis", (), {"eval": AsyncMock(side_effect=OSError("offline"))})()
    monkeypatch.setattr("app.cache.redis_client", redis)
    with pytest.raises(OSError):
        await limiter.reserve("scope", 1)
    assert limiter._local == {}


@pytest.mark.asyncio
async def test_slow_daily_admission_cannot_expire_or_shorten_sent_minute_usage(quota):
    limiter, now = quota
    token, _ = await limiter.reserve("scope", 1, pending_for=120)
    now[0] = 161.0
    # Another worker still sees the held slot before the first dispatch.
    assert await limiter.reserve("scope", 1) == (None, 60.0)
    await limiter.commit("scope", token)
    assert await limiter.reserve("scope", 1) == (None, 60.0)
    now[0] = 220.0
    assert await limiter.reserve("scope", 1) == (None, 1.0)
    now[0] = 221.0
    assert (await limiter.reserve("scope", 1))[0] is not None

"""Shared minute admission protects all users of the Gemini quota boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repos import keys


@pytest.fixture
def admission(monkeypatch):
    from app.providers.gemini_quota import GeminiMinuteQuota

    limiter = GeminiMinuteQuota()
    monkeypatch.setattr("app.cache.redis_client", None)
    monkeypatch.setattr(keys, "gemini_minute_quota", limiter)
    monkeypatch.setattr(keys, "get_model_minute_limit", AsyncMock(return_value=2))
    monkeypatch.setattr(keys, "get_model_daily_limit", AsyncMock(return_value=None))
    daily = AsyncMock(return_value=1)
    monkeypatch.setattr(keys._gemini_km, "reserve_usage", daily)
    monkeypatch.setattr(keys, "invalidate_key_cache", AsyncMock())
    return limiter, daily


@pytest.mark.asyncio
async def test_atomic_minute_admission_never_charges_daily_for_refused_requests(admission):
    _, daily = admission
    results = await asyncio.gather(*(keys.reserve_gemini_key_usage("key", "model") for _ in range(20)))
    assert sum(results) == 2
    assert daily.await_count == 2


@pytest.mark.asyncio
async def test_daily_refusal_releases_minute_slot(admission):
    limiter, daily = admission
    daily.return_value = None
    assert not await keys.reserve_gemini_key_usage("key", "model")
    assert await limiter.available("key:model", 1)


@pytest.mark.asyncio
async def test_minute_window_expires_and_separates_keys_and_models(admission, monkeypatch):
    limiter, _ = admission
    clock = [100.0]
    monkeypatch.setattr("app.providers.tts_quota.time.monotonic", lambda: clock[0])
    assert await keys.reserve_gemini_key_usage("key", "model")
    assert await keys.reserve_gemini_key_usage("key", "model")
    assert not await limiter.available("key:model", 2)
    assert await keys.reserve_gemini_key_usage("other", "model")
    assert await keys.reserve_gemini_key_usage("key", "other")
    clock[0] = 160.0
    assert await limiter.available("key:model", 2)
    assert await keys.reserve_gemini_key_usage("key", "model")


@pytest.mark.asyncio
async def test_selection_skips_full_key_without_suspension(admission, monkeypatch):
    await keys.reserve_gemini_key_usage("full", "model")
    await keys.reserve_gemini_key_usage("full", "model")
    select = AsyncMock(side_effect=[{"key_hash": "full", "api_key": "a"}, {"key_hash": "free", "api_key": "b"}])
    monkeypatch.setattr(keys._gemini_km, "get_fresh_available_key", select)
    assert await keys._get_fresh_available_key("model") == {"key_hash": "free", "api_key": "b"}
    assert select.await_args.kwargs["excluded_hashes"] == {"full"}


@pytest.mark.asyncio
async def test_minute_limit_reads_current_admin_snapshot(monkeypatch):
    snapshot = SimpleNamespace(values={"model_rpm:gemini-3.5-flash-lite": 17})
    monkeypatch.setattr("app.runtime_settings.models.get_snapshot", AsyncMock(return_value=snapshot))
    assert await keys.get_model_minute_limit("gemini-3.5-flash-lite") == 17
    snapshot.values["model_rpm:gemini-3.5-flash-lite"] = None
    assert await keys.get_model_minute_limit("gemini-3.5-flash-lite") is None


@pytest.mark.asyncio
async def test_reserved_selection_rotates_after_concurrent_admission_loss(monkeypatch):
    monkeypatch.setattr(
        keys,
        "get_available_gemini_key",
        AsyncMock(side_effect=[{"key_hash": "raced", "api_key": "a"}, {"key_hash": "free", "api_key": "b"}]),
    )
    reserve = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(keys, "reserve_gemini_key_usage", reserve)
    assert await keys.reserve_available_gemini_key("model", excluded_hashes={"original"}) == {
        "key_hash": "free",
        "api_key": "b",
    }
    assert reserve.await_count == 2


@pytest.mark.asyncio
async def test_redis_outage_never_opens_local_capacity_or_charges_daily(admission, monkeypatch):
    limiter, daily = admission
    monkeypatch.setattr("app.cache.redis_client", SimpleNamespace(eval=AsyncMock(side_effect=ConnectionError())))
    with pytest.raises(ConnectionError):
        await keys.reserve_gemini_key_usage("key", "model")
    daily.assert_not_awaited()
    assert limiter._local == {}


@pytest.mark.asyncio
async def test_cancelled_uncertain_daily_write_retains_pending_minute_token(admission, monkeypatch):
    limiter, daily = admission
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    daily.side_effect = block
    monkeypatch.setattr(keys, "get_model_minute_limit", AsyncMock(return_value=1))
    task = asyncio.create_task(keys.reserve_gemini_key_usage("key", "model"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await limiter.available("key:model", 1)


@pytest.mark.asyncio
async def test_minute_only_does_not_charge_shared_daily_quota(admission):
    _, daily = admission
    assert await keys.reserve_gemini_minute_usage("key", "gemini-embedding-test")
    assert await keys.reserve_gemini_minute_usage("key", "gemini-embedding-test")
    assert not await keys.reserve_gemini_minute_usage("key", "gemini-embedding-test")
    daily.assert_not_awaited()

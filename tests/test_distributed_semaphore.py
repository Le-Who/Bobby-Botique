"""Admission failures must not bypass global capacity or leak local slots."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.adapters.concurrency import GlobalLLMSemaphore
from app.errors import UserLimitExceededError


@pytest.mark.asyncio
async def test_global_capacity_timeout_rejects_and_releases_local_slot():
    redis = AsyncMock()
    redis.eval.return_value = 0
    clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 61]), monotonic_ns=time.monotonic_ns, time=time.time)
    sem = GlobalLLMSemaphore(limit=1, timeout=60)
    with patch("app.cache.redis_client", redis), patch("app.adapters.concurrency.time", clock):
        with pytest.raises(UserLimitExceededError):
            async with sem:
                pytest.fail("Global capacity rejection must not admit local work")
    assert sem._local_semaphore._value == 1
    assert sem._waiting_count == 0


@pytest.mark.asyncio
async def test_cancellation_during_redis_acquire_removes_token_and_releases_slot():
    redis = AsyncMock()
    checking_admission = asyncio.Event()

    async def blocked_admission(*args):
        checking_admission.set()
        await asyncio.Future()

    redis.eval.side_effect = blocked_admission
    sem = GlobalLLMSemaphore(limit=1)

    async def acquire():
        async with sem:
            pytest.fail("Cancelled acquisition must not enter")

    with patch("app.cache.redis_client", redis):
        task = asyncio.create_task(acquire())
        try:
            await asyncio.wait_for(checking_admission.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert sem._local_semaphore._value == 1
    token = redis.eval.call_args.args[-1]
    redis.zrem.assert_awaited_once_with(sem._key, token)


@pytest.mark.asyncio
async def test_redis_outage_still_allows_bounded_local_fallback():
    redis = AsyncMock()
    redis.eval.side_effect = ConnectionError("synthetic outage")
    sem = GlobalLLMSemaphore(limit=1)
    with patch("app.cache.redis_client", redis):
        async with sem:
            assert sem._local_semaphore._value == 0
    assert sem._local_semaphore._value == 1


@pytest.mark.asyncio
async def test_nested_different_semaphores_release_their_own_tokens():
    redis = AsyncMock()
    redis.eval.return_value = 1
    outer = GlobalLLMSemaphore(1, redis_key="outer")
    inner = GlobalLLMSemaphore(1, redis_key="inner")
    with patch("app.cache.redis_client", redis):
        async with outer, inner:
            pass
    tokens = {call.args[2]: call.args[-1] for call in redis.eval.call_args_list}
    assert {call.args for call in redis.zrem.call_args_list} == {("outer", tokens["outer"]), ("inner", tokens["inner"])}


@pytest.mark.asyncio
async def test_long_running_slot_is_renewed_and_renewal_stops_on_exit():
    redis = AsyncMock()
    redis.eval.return_value = 1
    renewed = asyncio.Event()

    async def eval_slot(*args):
        if len(args) == 4:
            renewed.set()
        return 1

    redis.eval.side_effect = eval_slot
    sem = GlobalLLMSemaphore(1)
    sem._renew_interval = 0.01
    with patch("app.cache.redis_client", redis):
        async with sem:
            await asyncio.wait_for(renewed.wait(), 0.2)
            renewal = sem._renewal.get()
            assert renewal is not None and not renewal.done()
        assert renewal.done()

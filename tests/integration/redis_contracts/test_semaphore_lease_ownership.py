"""Real Redis renewal return codes and fail-closed provider ownership."""

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.adapters.concurrency import _ADMIT_SLOT, _RENEW_SLOT, GlobalLLMSemaphore
from app.errors import UserLimitExceededError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def lease_server():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL must identify an explicitly disposable Redis")
    clients = [
        Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=1,
            retry=Retry(NoBackoff(), 0),
        )
        for _ in range(2)
    ]
    key = "contract:semaphore-lease:" + uuid.uuid4().hex
    try:
        for client in clients:
            assert await client.ping()
        yield clients, key
    finally:
        try:
            await clients[0].delete(key)
        finally:
            for client in clients:
                await client.aclose()


async def test_existing_member_renewal_returns_success_and_advances_server_score(lease_server):
    clients, key = lease_server
    await clients[0].zadd(key, {"owned-token": 1})
    before_s, before_us = await clients[1].time()

    assert await clients[0].eval(_RENEW_SLOT, 1, key, "owned-token") == 1

    after_s, after_us = await clients[1].time()
    score = await clients[1].zscore(key, "owned-token")
    assert before_s + before_us / 1_000_000 <= score <= after_s + after_us / 1_000_000
    assert await clients[1].zrange(key, 0, -1) == ["owned-token"]


async def test_absent_member_renewal_returns_lost_without_creating_key(lease_server):
    clients, key = lease_server

    assert await clients[0].eval(_RENEW_SLOT, 1, key, "missing-token") == 0
    assert not await clients[1].exists(key)


async def test_late_renewal_after_release_never_resurrects_or_removes_new_owner(lease_server):
    clients, key = lease_server
    assert await clients[0].eval(_ADMIT_SLOT, 1, key, 120, 1, "old-token") == 1
    assert await clients[0].zrem(key, "old-token") == 1
    assert await clients[1].eval(_ADMIT_SLOT, 1, key, 120, 1, "new-token") == 1

    assert await clients[0].eval(_RENEW_SLOT, 1, key, "old-token") == 0
    assert await clients[0].zrem(key, "old-token") == 0
    assert await clients[1].zrange(key, 0, -1) == ["new-token"]


async def test_lost_owner_joins_provider_and_preserves_new_limit_one_owner(lease_server, monkeypatch):
    clients, key = lease_server
    monkeypatch.setattr("app.cache.redis_client", clients[0])
    old = GlobalLLMSemaphore(1, redis_key=key)
    old._renew_interval = 0.01
    entered, stopping, finish_stop, stopped = (asyncio.Event() for _ in range(4))
    old_token = []
    old_renewal = []

    async def provider():
        try:
            await asyncio.Future()
        finally:
            stopping.set()
            await finish_stop.wait()
            stopped.set()

    async def old_holder():
        async with old:
            old_token.append(old._token.get())
            old_renewal.append(old._renewal.get())
            entered.set()
            await provider()
        pytest.fail("an owner with a lost Redis lease cannot report success")

    task = asyncio.create_task(old_holder())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await clients[1].zrem(key, old_token[0]) == 1
        await asyncio.wait_for(stopping.wait(), 1)
        assert not task.done() and not stopped.is_set()
        assert old._local_semaphore._value == 0

        monkeypatch.setattr("app.cache.redis_client", clients[1])
        new = GlobalLLMSemaphore(1, redis_key=key)
        async with new:
            new_token = new._token.get()
            finish_stop.set()
            with pytest.raises(UserLimitExceededError) as error:
                await task
            assert error.value.details["reason_code"] == "lease_lost"
            assert stopped.is_set() and old_renewal[0].done()
            assert await clients[1].zrange(key, 0, -1) == [new_token]
        assert await clients[1].zcard(key) == 0
        assert old._local_semaphore._value == new._local_semaphore._value == 1
    finally:
        finish_stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

"""Run the production queue fencing scripts on an explicitly isolated Redis."""

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis

from app import queue

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_queue_keys():
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is required for the real Redis integration")
    client = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    prefix = f"test:queue:{uuid.uuid4().hex}"
    keys = SimpleNamespace(
        lease=f"{prefix}:lease",
        owners=f"{prefix}:owners",
        pending=f"{prefix}:pending",
        processing=f"{prefix}:processing",
        retry=f"{prefix}:retry",
    )
    try:
        assert await client.ping(), "Isolated test Redis is unavailable"
        yield client, keys
    finally:
        try:
            # Never flush a shared database: remove only this fixture's UUID keys.
            await client.delete(*vars(keys).values())
        finally:
            await client.aclose()


async def test_acquire_lease_rejects_competing_owner(redis_queue_keys):
    redis, keys = redis_queue_keys
    assert await redis.eval(queue._ACQUIRE_LEASE, 2, keys.lease, keys.owners, "owner-a", 60) == 1
    assert await redis.eval(queue._ACQUIRE_LEASE, 2, keys.lease, keys.owners, "owner-b", 60) == 0
    assert await redis.get(keys.lease) == "owner-a"
    assert await redis.smembers(keys.owners) == {"owner-a"}
    assert 0 < await redis.ttl(keys.lease) <= 60


async def test_renew_and_release_are_owner_fenced(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.set(keys.lease, "owner-a", ex=10)
    assert await redis.eval(queue._RENEW_LEASE, 1, keys.lease, "owner-b", 60) == 0
    assert 0 < await redis.ttl(keys.lease) <= 10
    assert await redis.eval(queue._RENEW_LEASE, 1, keys.lease, "owner-a", 60) == 1
    assert 10 < await redis.ttl(keys.lease) <= 60
    assert await redis.eval(queue._RELEASE_LEASE, 1, keys.lease, "owner-b") == 0
    assert await redis.get(keys.lease) == "owner-a"
    assert await redis.eval(queue._RELEASE_LEASE, 1, keys.lease, "owner-a") == 1
    assert await redis.get(keys.lease) is None


async def test_claim_moves_one_task_only_for_current_owner(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.set(keys.lease, "owner-a", ex=60)
    await redis.lpush(keys.pending, "first-task", "second-task")
    assert await redis.eval(queue._CLAIM_TASK, 3, keys.lease, keys.pending, keys.processing, "owner-b") is None
    assert await redis.lrange(keys.pending, 0, -1) == ["second-task", "first-task"]
    assert await redis.llen(keys.processing) == 0
    assert await redis.eval(queue._CLAIM_TASK, 3, keys.lease, keys.pending, keys.processing, "owner-a") == "first-task"
    assert await redis.lrange(keys.pending, 0, -1) == ["second-task"]
    assert await redis.lrange(keys.processing, 0, -1) == ["first-task"]


@pytest.mark.parametrize("retry_payload", ["", "retry-task"])
async def test_finish_is_fenced_and_retry_is_enqueued_once(redis_queue_keys, retry_payload):
    redis, keys = redis_queue_keys
    await redis.set(keys.lease, "owner-a", ex=60)
    await redis.lpush(keys.processing, "in-flight")
    arguments = (keys.lease, keys.processing, keys.retry)
    assert await redis.eval(queue._FINISH_TASK, 3, *arguments, "owner-b", "in-flight", retry_payload) == 0
    assert await redis.lrange(keys.processing, 0, -1) == ["in-flight"]
    assert await redis.llen(keys.retry) == 0
    assert await redis.eval(queue._FINISH_TASK, 3, *arguments, "owner-a", "in-flight", retry_payload) == 1
    assert await redis.llen(keys.processing) == 0
    assert await redis.eval(queue._FINISH_TASK, 3, *arguments, "owner-a", "in-flight", retry_payload) == 0
    assert await redis.lrange(keys.retry, 0, -1) == ([retry_payload] if retry_payload else [])


async def test_recovery_waits_for_expired_owner_and_is_idempotent(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.set(keys.lease, "owner-a", ex=60)
    await redis.lpush(keys.processing, "orphan-task")
    arguments = (keys.lease, keys.processing, keys.pending, "orphan-task", "recovered-task", "owned")
    assert await redis.eval(queue._RECOVER_TASK, 3, *arguments) == 0
    assert await redis.lrange(keys.processing, 0, -1) == ["orphan-task"]
    assert await redis.llen(keys.pending) == 0
    # Expire the fixture's lease deterministically rather than waiting for TTL.
    await redis.delete(keys.lease)
    assert await redis.eval(queue._RECOVER_TASK, 3, *arguments) == 1
    assert await redis.eval(queue._RECOVER_TASK, 3, *arguments) == 0
    assert await redis.llen(keys.processing) == 0
    assert await redis.lrange(keys.pending, 0, -1) == ["recovered-task"]


async def test_exhausted_recovery_removes_processing_without_requeue(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.lpush(keys.processing, "exhausted-task")
    arguments = (keys.lease, keys.processing, keys.pending, "exhausted-task", "", "owned")
    assert await redis.eval(queue._RECOVER_TASK, 3, *arguments) == 1
    assert await redis.eval(queue._RECOVER_TASK, 3, *arguments) == 0
    assert await redis.llen(keys.processing) == 0
    assert await redis.llen(keys.pending) == 0


@pytest.mark.parametrize("task_type", ["deferred_ai_response", "document_processing"])
async def test_explicit_drained_legacy_recovery_preserves_effects_without_replay(
    redis_queue_keys, monkeypatch, task_type
):
    from app import queue_legacy

    redis, keys = redis_queue_keys
    monkeypatch.setattr(queue_legacy, "_PROCESSING_KEY", keys.processing)
    monkeypatch.setattr(queue_legacy, "_LEASE_PREFIX", keys.lease)
    monkeypatch.setattr(queue_legacy, "_OWNERS_KEY", keys.owners)
    monkeypatch.setattr(queue_legacy, "_QUEUE_PREFIX", keys.pending)
    processing = f"{keys.processing}:dead"
    pending = f"{keys.pending}:2"
    task = queue.Task(
        id="interrupted-deferred",
        user_id=1,
        task_type=task_type,
        data={},
        priority=queue.TaskPriority.NORMAL,
        status=queue.TaskStatus.PENDING,
        created_at=datetime.now(UTC),
    )
    try:
        await redis.sadd(keys.owners, "dead")
        await redis.lpush(processing, queue._task_to_json_bytes(task))
        with pytest.raises(ValueError, match="legacy_drain_required"):
            await queue_legacy.recover_legacy_processing(redis)
        assert await queue_legacy.recover_legacy_processing(redis, confirm_drained=True) == 0
        assert await queue_legacy.recover_legacy_processing(redis, confirm_drained=True) == 0
        assert await redis.llen(processing) == 1
        assert await redis.llen(pending) == 0
    finally:
        await redis.delete(processing, pending)


async def test_legacy_recovery_does_not_require_an_owner_lease(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.set(keys.lease, "unrelated-current-owner", ex=60)
    await redis.lpush(keys.processing, "legacy-task")
    assert (
        await redis.eval(
            queue._RECOVER_TASK,
            3,
            keys.lease,
            keys.processing,
            keys.pending,
            "legacy-task",
            "recovered-legacy-task",
            "legacy",
        )
        == 1
    )
    assert await redis.lrange(keys.pending, 0, -1) == ["recovered-legacy-task"]
    assert await redis.get(keys.lease) == "unrelated-current-owner"


async def test_owner_is_removed_only_after_lease_and_processing_are_empty(redis_queue_keys):
    redis, keys = redis_queue_keys
    await redis.sadd(keys.owners, "owner-a", "owner-b")
    await redis.set(keys.lease, "owner-a", ex=60)
    arguments = (keys.lease, keys.processing, keys.owners, "owner-a")
    assert await redis.eval(queue._REMOVE_OWNER, 3, *arguments) == 0
    await redis.delete(keys.lease)
    await redis.lpush(keys.processing, "in-flight")
    assert await redis.eval(queue._REMOVE_OWNER, 3, *arguments) == 0
    await redis.delete(keys.processing)
    assert await redis.eval(queue._REMOVE_OWNER, 3, *arguments) == 1
    assert await redis.smembers(keys.owners) == {"owner-b"}

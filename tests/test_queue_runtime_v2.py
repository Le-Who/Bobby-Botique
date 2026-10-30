import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.queue import TaskQueue
from app.queue_contracts import AdmissionState, JobOutcome, JobResult, JobState
from app.queue_store import MemoryQueueStore


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", scope)


async def test_actual_worker_publishes_shared_state_and_cancellation_blocks_next_effect():
    store = MemoryQueueStore()
    producer, worker = TaskQueue(store=store), TaskQueue(store=store)
    started, continue_work = asyncio.Event(), asyncio.Event()
    effects = []

    async def handler():
        from app.queue import current_job_guard

        started.set()
        await continue_work.wait()
        await current_job_guard().checkpoint()
        effects.append("delivered")
        return JobResult(JobOutcome.SUCCEEDED)

    worker.register_handler("test_guard", handler, lane="deferred")
    producer.register_handler("test_guard", handler, lane="deferred")
    admission = await producer.enqueue_job(0, "test_guard", {}, idempotency_key="operation", lane="deferred")
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 1)
        running = await producer.read_job(admission.task_id)
        assert running.state is JobState.RUNNING
        cancelled = await producer.request_cancel(
            admission.task_id, expected_revision=running.revision, actor="admin", operation_id="cancel"
        )
        assert cancelled.state is JobState.CANCEL_REQUESTED
        continue_work.set()
        for _ in range(100):
            final = await producer.read_job(admission.task_id)
            if final.state is JobState.CANCELLED:
                break
            await asyncio.sleep(0.01)
        assert final.state is JobState.CANCELLED
        assert effects == []
    finally:
        await worker.stop()


async def test_actual_worker_cancel_during_uncertain_effect_is_unknown(monkeypatch):
    monkeypatch.setattr("app.queue_runtime.OWNER_HEARTBEAT_SECONDS", 0.01)
    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    sending = asyncio.Event()
    effects = []

    async def handler():
        effects.append("synthetic Telegram send attempted")
        sending.set()
        await asyncio.Future()

    queue.register_handler("test_uncertain_send", handler, lane="deferred")
    job = await queue.enqueue_job(0, "test_uncertain_send", {}, idempotency_key="operation", lane="deferred")
    await queue.start()
    try:
        await asyncio.wait_for(sending.wait(), 1)
        current = await queue.read_job(job.task_id)
        await queue.request_cancel(
            job.task_id, expected_revision=current.revision, actor="admin", operation_id="cancel"
        )
        for _ in range(100):
            final = await queue.read_job(job.task_id)
            if final.state in {JobState.UNKNOWN_DELIVERY, JobState.CANCELLED}:
                break
            await asyncio.sleep(0.01)
        assert final.state is JobState.UNKNOWN_DELIVERY
        assert len(effects) == 1
        assert not final.retry_payload_available
    finally:
        await queue.stop()


async def test_three_delayed_jobs_leave_maintenance_worker_free():
    queue = TaskQueue(store=MemoryQueueStore())
    finished = asyncio.Event()

    async def cleanup():
        finished.set()
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_cleanup", cleanup, lane="maintenance", effectful=False)
    for index in range(3):
        admission = await queue.enqueue_job(
            0, "deferred_ai_response", {}, idempotency_key=str(index), lane="deferred", delay_seconds=15
        )
        assert admission.state is AdmissionState.ACCEPTED
    await queue.enqueue_job(0, "test_cleanup", {}, idempotency_key="cleanup", lane="maintenance")
    await queue.start()
    try:
        await asyncio.wait_for(finished.wait(), 1)
        stats = await queue.get_queue_stats()
        assert stats["lanes"]["deferred"]["delayed"] == 3
        assert stats["lanes"]["deferred"]["running"] == 0
    finally:
        await queue.stop()


async def test_unknown_kind_rejected_before_any_storage_mutation():
    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    result = await queue.enqueue_job(0, "arbitrary_callable", {}, idempotency_key="one", lane="deferred")
    assert result.state is AdmissionState.REJECTED
    assert not store.jobs


async def test_dashboard_read_before_redis_initialization_does_not_pin_local_backend(monkeypatch):
    from app.queue_store import RedisQueueStore
    from tests.test_redis_queue import WireRedis

    client = [None]
    monkeypatch.setattr("app.queue._get_redis", lambda: client[0])
    queue = TaskQueue()
    early = await queue.get_queue_stats()
    assert early["available"] is False
    assert queue._store is None
    client[0] = WireRedis()
    await queue.start()
    try:
        assert isinstance(queue._store, RedisQueueStore)
        assert (await queue.get_queue_stats())["backend"] == "redis"
    finally:
        await queue.stop()


async def test_malformed_payload_is_metadata_quarantined_counted_and_never_executed():
    from app.queue_contracts import QueuePolicy
    from app.queue_store import JobEnvelope

    store = MemoryQueueStore()
    queue = TaskQueue(store=store)
    effects = []

    async def handler():
        effects.append("should-not-run")
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_malformed", handler, lane="deferred")
    await store.admit(JobEnvelope("bad", "test_malformed", "deferred", 0, "{broken-json", "bad"), QueuePolicy())
    await queue.start()
    try:
        for _ in range(100):
            snapshot = await queue.read_job("bad")
            if snapshot.state is JobState.FAILED:
                break
            await asyncio.sleep(0.01)
        assert snapshot.reason_code == "invalid_job_payload"
        assert not effects and not store.payloads
        assert (await store.stats("deferred"))["malformed_payloads"] == 1
    finally:
        await queue.stop()


async def test_ten_thousand_mixed_admissions_outage_and_drain_stay_bounded(monkeypatch):
    from app.queue_store import RedisQueueStore
    from tests.test_redis_queue import WireRedis

    redis = WireRedis()
    redis.fail_after.add("admit")
    monkeypatch.setattr("app.queue._get_redis", lambda: redis)
    queue = TaskQueue(store=RedisQueueStore(redis), clock=lambda: redis.clock[0])
    calls = []

    async def handler(index):
        calls.append(index)
        return JobResult(JobOutcome.SUCCEEDED)

    lanes = ("deferred", "maintenance", "daily")
    for lane in lanes:
        queue.register_handler(f"test_{lane}", handler, lane=lane, effectful=False)
    unknown = 0
    for index in range(10_000):
        redis.available = not 2000 <= index < 2040
        lane = lanes[index % 3]
        result = await queue.enqueue_job(
            0, f"test_{lane}", {"index": index}, idempotency_key=f"operation-{index}", lane=lane
        )
        unknown += result.state is AdmissionState.UNKNOWN
    redis.available = True
    assert unknown == 40
    assert len(redis.model.jobs) == 232
    assert len(queue.tasks) <= 256
    for lane, limit in (("deferred", 100), ("maintenance", 100), ("daily", 32)):
        stats = await queue._store.stats(lane)
        assert stats["nonterminal"] == limit
        assert stats["payload_bytes"] <= 8 * 1024 * 1024
    await queue.start()
    try:
        for _ in range(300):
            if len(calls) == 232:
                break
            await asyncio.sleep(0.01)
        for _ in range(100):
            if not queue._executions:
                break
            await asyncio.sleep(0.01)
        assert len(calls) == len(set(calls)) == 232
        assert not redis.model.payloads
        for lane in lanes:
            assert (await queue._store.stats(lane))["nonterminal"] == 0
        assert len(queue.tasks) <= 256
    finally:
        await queue.stop()
    assert not queue.workers and not queue._executions

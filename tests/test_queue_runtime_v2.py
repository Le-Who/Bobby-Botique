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


async def test_ten_thousand_mixed_admissions_outage_and_drain_stay_bounded(monkeypatch, caplog):
    import logging

    from app.queue_store import RedisQueueStore
    from tests.test_redis_queue import WireRedis

    # Capacity and recovery are the subject here. Logging integration tests can
    # leave INFO handlers installed; retain warnings/errors without turning
    # 10,000 admissions into a synchronous log-writer throughput benchmark.
    caplog.set_level(logging.WARNING, logger="app.observability")
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


async def test_idle_workers_back_off_without_scheduler_wake_storm():
    class CountingStore(MemoryQueueStore):
        claims = 0
        sweeps = 0

        async def claim(self, lane, owner_token):
            self.claims += 1
            return await super().claim(lane, owner_token)

        async def sweep(self):
            self.sweeps += 1
            await super().sweep()

    store = CountingStore()
    queue = TaskQueue(store=store)
    await queue.start()
    try:
        await asyncio.sleep(3.2)
        # Seven workers get an initial probe and bounded early discovery probes.
        assert 7 <= store.claims <= 28
        assert store.sweeps <= 2
    finally:
        await queue.stop()


async def test_admission_while_empty_claim_returns_cannot_lose_local_wakeup():
    empty_claim, release_claim, handled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class PausedStore(MemoryQueueStore):
        async def claim(self, lane, owner_token):
            claim = await super().claim(lane, owner_token)
            if lane == "deferred" and not empty_claim.is_set():
                empty_claim.set()
                await release_claim.wait()
            return claim

    queue = TaskQueue(max_workers=1, store=PausedStore())

    async def handler():
        handled.set()
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_local_wakeup", handler, lane="deferred", effectful=False)
    await queue.start()
    try:
        await asyncio.wait_for(empty_claim.wait(), 1)
        await queue.enqueue_job(0, "test_local_wakeup", {}, idempotency_key="race", lane="deferred")
        release_claim.set()
        await asyncio.wait_for(handled.wait(), 0.2)
    finally:
        release_claim.set()
        await queue.stop()


async def test_local_admission_wakes_its_lane_without_probing_other_lanes():
    class CountingStore(MemoryQueueStore):
        def __init__(self):
            super().__init__()
            self.probes = dict.fromkeys(("deferred", "maintenance", "daily", "broadcast"), 0)

        async def claim(self, lane, owner_token):
            self.probes[lane] += 1
            return await super().claim(lane, owner_token)

    store, handled = CountingStore(), asyncio.Event()
    queue = TaskQueue(store=store)

    async def handler():
        handled.set()
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_lane_wakeup", handler, lane="maintenance", effectful=False)
    await queue.start()
    try:
        await asyncio.sleep(1.6)
        before = dict(store.probes)
        await queue.enqueue_job(0, "test_lane_wakeup", {}, idempotency_key="maintenance", lane="maintenance")
        await asyncio.wait_for(handled.wait(), 0.2)
        assert store.probes["maintenance"] > before["maintenance"]
        for lane in ("deferred", "daily", "broadcast"):
            assert store.probes[lane] == before[lane]
    finally:
        await queue.stop()


async def test_local_burst_wakes_every_worker_in_the_lane():
    queue = TaskQueue(store=MemoryQueueStore())
    started, all_started, release = [], asyncio.Event(), asyncio.Event()

    async def handler(index):
        started.append(index)
        if len(started) == 3:
            all_started.set()
        await release.wait()
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_parallel_wakeup", handler, lane="deferred", effectful=False)
    await queue.start()
    try:
        await asyncio.sleep(1.6)
        for index in range(3):
            await queue.enqueue_job(
                0, "test_parallel_wakeup", {"index": index}, idempotency_key=str(index), lane="deferred"
            )
        await asyncio.wait_for(all_started.wait(), 0.2)
        assert sorted(started) == [0, 1, 2]
    finally:
        release.set()
        await queue.stop()


async def test_max_idle_backoff_discovers_remote_delayed_and_expired_owner_work_within_five_seconds():
    from app.queue_contracts import QueuePolicy
    from app.queue_store import JobEnvelope

    clock, idle_backoff = [1000.0], asyncio.Event()

    class ObservedStore(MemoryQueueStore):
        probes = 0

        async def claim(self, lane, owner_token):
            result = await super().claim(lane, owner_token)
            if lane == "deferred":
                self.probes += 1
                if self.probes == 5:
                    idle_backoff.set()
            return result

    store = ObservedStore(clock=lambda: clock[0])
    queue = TaskQueue(max_workers=1, store=store, clock=lambda: clock[0])
    effects, finished = [], asyncio.Event()

    async def handler(source):
        effects.append(source)
        if len(effects) == 3:
            finished.set()
        return JobResult(JobOutcome.SUCCEEDED)

    for lane in ("deferred", "daily", "maintenance", "broadcast"):
        queue.register_handler("test_discovery_" + lane, handler, lane=lane, effectful=lane == "broadcast")
    for task_id, lane, delay, effectful in (
        ("delayed", "daily", 61, False),
        ("recoverable", "maintenance", 0, False),
        ("uncertain", "broadcast", 0, True),
    ):
        await store.admit(
            JobEnvelope(
                task_id,
                "test_discovery_" + lane,
                lane,
                0,
                '{"data":{"source":"' + task_id + '"},"context":{}}',
                task_id,
                delay_seconds=delay,
                effectful=effectful,
            ),
            QueuePolicy(),
        )
        if not delay:
            await store.claim(lane, "dead-owner")
    await queue.start()
    try:
        # The fifth empty probe follows 0.5, 1, 2, and 4 second waits. No local
        # admission notification is sent to this queue for the new remote job.
        await asyncio.wait_for(idle_backoff.wait(), 9)
        await store.admit(
            JobEnvelope(
                "remote",
                "test_discovery_deferred",
                "deferred",
                0,
                '{"data":{"source":"remote"},"context":{}}',
                "remote",
                effectful=False,
            ),
            QueuePolicy(),
        )
        clock[0] = 1061.0
        await asyncio.wait_for(finished.wait(), 5.2)
        assert sorted(effects) == ["delayed", "recoverable", "remote"]
        assert store.jobs["uncertain"]["state"] == "unknown_delivery"
        assert store.jobs["recoverable"]["attempts_started"] == 2
        assert store.jobs["uncertain"]["attempts_started"] == 1
    finally:
        await queue.stop()

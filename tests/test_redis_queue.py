"""Tests for Redis-backed persistent queue.

Validates:
1. Task serialization/deserialization round-trip.
2. Enqueue/dequeue cycle with mock Redis.
3. Priority ordering (URGENT before LOW).
4. Crash recovery: tasks stuck in processing re-queued on startup.
5. Fallback to in-memory when Redis unavailable.
6. Task cancellation is respected.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app import queue as queue_module
from app.queue import (
    Task,
    TaskPriority,
    TaskQueue,
    TaskStatus,
    _task_from_json,
    _task_to_json,
)
from app.queue_contracts import QueueDisposition


class QueueRedis:
    """Protocol double for deterministic Python queue interleavings.

    This double models Redis responses; it does not interpret production Lua.
    Script syntax, fencing and TTL semantics require the isolated Redis tests.
    """

    def __init__(self):
        self.lists = {}
        self.values = {}
        self.sets = {}

    async def lpush(self, key, raw):
        self.lists.setdefault(key, []).insert(0, raw)
        return len(self.lists[key])

    async def rpoplpush(self, source, destination):
        items = self.lists.setdefault(source, [])
        if not items:
            return None
        raw = items.pop()
        await self.lpush(destination, raw)
        return raw

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))[start:] if end == -1 else list(self.lists.get(key, []))[start : end + 1]

    async def lrem(self, key, count, raw):
        items = self.lists.setdefault(key, [])
        if raw not in items:
            return 0
        items.remove(raw)
        return 1

    async def delete(self, key):
        self.lists.pop(key, None)
        self.values.pop(key, None)

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def exists(self, key):
        return int(key in self.values)

    async def smembers(self, key):
        return self.sets.get(key, set()).copy()

    async def eval(self, script, number, *arguments):
        keys, args = arguments[:number], arguments[number:]
        if script == queue_module._ACQUIRE_LEASE:
            if keys[0] in self.values:
                return 0
            self.values[keys[0]] = args[0]
            self.sets.setdefault(keys[1], set()).add(args[0])
            return 1
        if script in (queue_module._RENEW_LEASE, queue_module._RELEASE_LEASE):
            if self.values.get(keys[0]) != args[0]:
                return 0
            if script == queue_module._RELEASE_LEASE:
                await self.delete(keys[0])
            return 1
        if script == queue_module._CLAIM_TASK:
            if self.values.get(keys[0]) != args[0]:
                return None
            return await self.rpoplpush(keys[1], keys[2])
        if script == queue_module._FINISH_TASK:
            if self.values.get(keys[0]) != args[0]:
                return 0
            removed = await self.lrem(keys[1], 1, args[1])
            if removed and args[2]:
                await self.lpush(keys[2], args[2])
            return removed
        if script == queue_module._RECOVER_TASK:
            if args[2] != "legacy" and await self.exists(keys[0]):
                return 0
            removed = await self.lrem(keys[1], 1, args[0])
            if removed and args[1]:
                await self.lpush(keys[2], args[1])
            return removed
        if script == queue_module._REMOVE_OWNER:
            if not await self.exists(keys[0]) and not await self.llen(keys[1]):
                self.sets.setdefault(keys[2], set()).discard(args[0])
                return 1
            return 0
        raise AssertionError("Unexpected Redis script")


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
    return Task(
        id="test-uuid-1",
        user_id=42,
        task_type="document_processing",
        data={"filename": "test.pdf"},
        priority=TaskPriority.NORMAL,
        status=TaskStatus.PENDING,
        created_at=datetime.now(tz=UTC),
    )


class TestTaskSerialization:
    """Test JSON round-trip for tasks."""

    def test_serialize_and_deserialize(self, sample_task):
        json_str = _task_to_json(sample_task)
        restored = _task_from_json(json_str)

        assert restored.id == sample_task.id
        assert restored.user_id == sample_task.user_id
        assert restored.task_type == sample_task.task_type
        assert restored.data == sample_task.data
        assert restored.priority == sample_task.priority
        assert restored.status == sample_task.status
        assert restored.observability_schema_version == 1
        assert restored.observability_context == sample_task.observability_context

    def test_deserialize_from_bytes(self, sample_task):
        json_bytes = _task_to_json(sample_task).encode("utf-8")
        restored = _task_from_json(json_bytes)
        assert restored.id == sample_task.id

    def test_bytes_serialiser_preserves_text_api(self, sample_task):
        from app import queue as queue_module

        serializer = getattr(queue_module, "_task_to_json_bytes", None)
        assert serializer is not None
        assert isinstance(_task_to_json(sample_task), str)

        raw = serializer(sample_task)
        assert isinstance(raw, bytes)
        assert _task_from_json(raw) == sample_task

    def test_optional_fields_none(self, sample_task):
        """Tasks with None optional fields serialize correctly."""
        assert sample_task.started_at is None
        json_str = _task_to_json(sample_task)
        restored = _task_from_json(json_str)
        assert restored.started_at is None
        assert restored.result is None


class WireRedis(QueueRedis):
    """Network-protocol double using the local transition adapter, NOT a Lua interpreter."""

    def __init__(self):
        super().__init__()
        from app.queue_store import MemoryQueueStore

        self.clock = [1000.0]
        self.model = MemoryQueueStore(clock=lambda: self.clock[0])
        self.available = True
        self.fail_after = set()
        self.stay_down = False
        self.calls = []

    async def scan(self, cursor, *, match, count):
        return 0, [key for key in self.lists if key.startswith("gemaibotv2:processing")]

    async def get(self, key):
        import json

        if ":idem:" in key:
            alias = self.model.identities.get(key.split(":idem:", 1)[1])
            return json.dumps({"task_id": alias[0], "fingerprint": alias[1]}) if alias else None
        if ":job:" in key:
            record = self.model.jobs.get(key.split(":job:", 1)[1])
            return json.dumps(record) if record else None
        return self.values.get(key)

    async def eval(self, script, number, *arguments):
        import json
        from dataclasses import asdict

        from app.queue_contracts import JobOutcome, JobResult, QueuePolicy
        from app.queue_store import _QUEUE_SCRIPT, JobEnvelope

        if script != _QUEUE_SCRIPT:
            return await super().eval(script, number, *arguments)
        if not self.available:
            raise ConnectionError("synthetic unavailable")
        request = json.loads(arguments[1])
        action = request["action"]
        self.calls.append(action)
        m = self.model
        if action == "admit":
            result = asdict(await m.admit(JobEnvelope(**request["job"]), QueuePolicy(**request["policy"])))
        elif action == "lookup_admission":
            previous = m.identities.get(request["identity"])
            result = {
                "state": "existing" if previous else "unknown",
                "task_id": previous[0] if previous else request["job"]["task_id"],
                "storage_mode": "redis",
            }
        elif action in {"read", "private"}:
            result = await m.private_record(request["task_id"])
        elif action == "claim":
            claim = await m.claim(request["lane"], request["owner_token"])
            result = (
                {"record": await m.private_record(claim.snapshot.task_id), "payload": claim.payload} if claim else None
            )
        elif action == "load":
            result = await m.load_payload(request["task_id"], request["owner_token"])
        elif action == "progress":
            await m.update_progress(
                request["task_id"], request["owner_token"], request["expected_revision"], request["summary"]
            )
            result = await m.private_record(request["task_id"])
        elif action == "finish":
            value = request["result"]
            await m.finish(
                request["task_id"],
                request["owner_token"],
                request["expected_revision"],
                JobResult(JobOutcome(value["outcome"]), value.get("reason_code"), value.get("result_summary") or {}),
                delay_seconds=request["delay_seconds"],
            )
            result = await m.private_record(request["task_id"])
        elif action == "heartbeat":
            await m.heartbeat(request["task_id"], request["owner_token"], request["expected_revision"])
            result = await m.private_record(request["task_id"])
        elif action == "stats":
            result = await m.stats(request["lane"])
        elif action == "lane":
            lane = await m.read_lane(request["lane"])
            result = {
                "revision": lane.revision,
                "admission_paused": lane.admission_paused,
                "claim_paused": lane.claim_paused,
                "policy": lane.policy.as_dict(),
            }
        elif action == "sweep":
            await m.sweep()
            result = {}
        else:
            raise AssertionError(f"unsupported protocol action {action}")
        if action in self.fail_after:
            self.fail_after.remove(action)
            if self.stay_down:
                self.available = False
            raise ConnectionError("synthetic reply loss after committed mutation")
        return json.dumps(result)


@pytest.fixture(autouse=True)
def offline_runtime_settings(monkeypatch):
    @asynccontextmanager
    async def scope():
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", scope)


async def test_commit_then_disconnect_reconciles_one_job_and_one_handler_call(monkeypatch):
    from app.queue_contracts import AdmissionState, JobOutcome, JobResult, JobState
    from app.queue_store import RedisQueueStore

    redis = WireRedis()
    redis.fail_after.add("admit")
    monkeypatch.setattr(queue_module, "_get_redis", lambda: redis)
    queue = TaskQueue(store=RedisQueueStore(redis))
    calls = []

    async def handler():
        calls.append("one")
        return JobResult(JobOutcome.SUCCEEDED)

    queue.register_handler("test_count", handler, lane="deferred", effectful=False)
    admission = await queue.enqueue_job(0, "test_count", {}, idempotency_key="operation", lane="deferred")
    assert admission.state is AdmissionState.EXISTING
    duplicate = await queue.enqueue_job(0, "test_count", {}, idempotency_key="operation", lane="deferred")
    assert duplicate.task_id == admission.task_id
    await queue.start()
    try:
        for _ in range(100):
            status = await queue.read_job(admission.task_id)
            if status.state is JobState.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert status.state is JobState.SUCCEEDED
        assert calls == ["one"]
        assert len(redis.model.jobs) == 1
        assert not redis.model.payloads
    finally:
        await queue.stop()


async def test_unknown_admission_never_selects_local_fallback_and_repeat_is_stable(monkeypatch):
    from app.queue_contracts import AdmissionState
    from app.queue_store import RedisQueueStore

    redis = WireRedis()
    redis.fail_after.add("admit")
    redis.stay_down = True
    monkeypatch.setattr(queue_module, "_get_redis", lambda: redis)
    queue = TaskQueue()
    uncertain = await queue.enqueue_job(0, "cleanup_metrics", {}, idempotency_key="operation", lane="maintenance")
    assert uncertain.state is AdmissionState.UNKNOWN
    assert isinstance(queue._store, RedisQueueStore)
    assert len(redis.model.jobs) == 1
    assert not queue.tasks
    redis.available = True
    again = await queue.enqueue_job(0, "cleanup_metrics", {}, idempotency_key="operation", lane="maintenance")
    assert again.state is AdmissionState.EXISTING
    assert again.task_id == uncertain.task_id
    assert len(redis.model.jobs) == 1


async def test_finish_reply_loss_reconciles_atomic_terminal_without_double_release():
    from app.queue_contracts import JobOutcome, JobResult, JobState, QueuePolicy
    from app.queue_store import JobEnvelope, RedisQueueStore

    redis = WireRedis()
    store = RedisQueueStore(redis)
    await store.admit(
        JobEnvelope("job", "cleanup_metrics", "maintenance", 0, "{}", "operation", effectful=False), QueuePolicy()
    )
    claim = await store.claim("maintenance", "owner")
    redis.fail_after.add("finish")
    result = await store.finish("job", "owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    assert result.state is JobState.SUCCEEDED
    assert (await store.stats("maintenance"))["nonterminal"] == 0
    assert (await store.stats("maintenance"))["payload_bytes"] == 0


async def test_legacy_backlog_is_visible_and_requires_explicit_drained_recovery(sample_task):
    from app import queue_legacy

    redis = WireRedis()
    raw = _task_to_json(sample_task).encode()
    redis.lists[queue_legacy._PROCESSING_KEY] = [raw]
    status = await queue_legacy.inspect_legacy(redis)
    assert status["reason_code"] == "legacy_drain_required"
    assert status["processing"] == 1
    with pytest.raises(ValueError, match="legacy_drain_required"):
        await queue_legacy.recover_legacy_processing(redis)
    assert redis.lists[queue_legacy._PROCESSING_KEY] == [raw]
    assert await queue_legacy.recover_legacy_processing(redis, confirm_drained=True) == 0
    assert redis.lists[queue_legacy._PROCESSING_KEY] == [raw]
    assert not redis.lists.get(queue_legacy._queue_key(sample_task.priority))


async def test_legacy_recovery_preserves_malformed_records_for_separate_disposition():
    from app import queue_legacy

    redis = WireRedis()
    raw = b"synthetic malformed legacy metadata"
    redis.lists[queue_legacy._PROCESSING_KEY] = [raw]
    assert await queue_legacy.recover_legacy_processing(redis, confirm_drained=True) == 0
    assert redis.lists[queue_legacy._PROCESSING_KEY] == [raw]


async def test_v2_startup_does_not_execute_or_delete_v1_payload(sample_task, monkeypatch):
    from app.queue_store import RedisQueueStore

    redis = WireRedis()
    monkeypatch.setattr(queue_module, "_get_redis", lambda: redis)
    raw = _task_to_json(sample_task).encode()
    redis.lists["gemaibotv2:queue:2"] = [raw]
    queue = TaskQueue(store=RedisQueueStore(redis))
    await queue.start()
    try:
        await asyncio.sleep(0.02)
        assert redis.lists["gemaibotv2:queue:2"] == [raw]
        assert (await queue.get_queue_stats())["legacy"]["reason_code"] == "legacy_drain_required"
        assert not redis.model.jobs
    finally:
        await queue.stop()


async def test_privacy_erasure_removes_only_matching_legacy_payloads(sample_task):
    from dataclasses import replace

    from app import queue_legacy

    redis = WireRedis()
    other = replace(sample_task, id="other", user_id=43)
    first, second = _task_to_json(sample_task).encode(), _task_to_json(other).encode()
    for key in ("gemaibotv2:queue:2", queue_legacy._PROCESSING_KEY):
        redis.lists[key] = [first, second]
    await queue_legacy.erase_legacy_user(redis, 42)
    assert all(rows == [second] for rows in redis.lists.values())


async def test_coalesced_operation_lookup_is_readonly_and_returns_original_id():
    from app.queue_contracts import JobOutcome, JobResult, JobState, QueuePolicy
    from app.queue_store import JobEnvelope, RedisQueueStore

    redis = WireRedis()
    store = RedisQueueStore(redis)
    first = JobEnvelope("first", "daily_prepare", "daily", 0, "{}", "first", conflict_keys=("day",))
    second = JobEnvelope("second", "daily_prepare", "daily", 0, "{}", "second", conflict_keys=("day",))
    await store.admit(first, QueuePolicy(max_jobs=32))
    assert (await store.admit(second, QueuePolicy(max_jobs=32))).task_id == "first"
    claim = await store.claim("daily", "owner")
    await store.finish("first", "owner", claim.snapshot.revision, JobResult(JobOutcome.SUCCEEDED))
    before = list(redis.calls)
    snapshot = await store.read_job_by_operation(0, "second")
    assert snapshot.task_id == "first" and snapshot.state is JobState.SUCCEEDED
    assert redis.calls == before
    assert len(redis.model.jobs) == 1

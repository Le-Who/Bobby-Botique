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
        return list(self.lists.get(key, []))

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
            if removed:
                await self.lpush(keys[2], args[1])
            return removed
        if script == queue_module._REMOVE_OWNER:
            if not await self.exists(keys[0]) and not await self.llen(keys[1]):
                self.sets.setdefault(keys[2], set()).discard(args[0])
                return 1
            return 0
        raise AssertionError("Unexpected Redis script")


@pytest.mark.asyncio
async def test_startup_recovery_does_not_steal_another_replica_task(sample_task):
    redis = QueueRedis()
    with patch("app.queue._get_redis", return_value=redis):
        first = TaskQueue(max_workers=1)
        second = TaskQueue(max_workers=1)
        first._use_redis = second._use_redis = True
        await redis.lpush("gemaibotv2:queue:2", _task_to_json(sample_task).encode())
        task, original = await first._dequeue_task()
        assert task is not None

        await second._recover_processing_tasks()

        assert redis.lists["gemaibotv2:queue:2"] == []
        assert original in [raw for items in redis.lists.values() for raw in items]


@pytest.mark.asyncio
async def test_legacy_processing_requires_explicit_drained_worker_recovery(sample_task):
    redis = QueueRedis()
    original = _task_to_json(sample_task).encode()
    redis.lists[queue_module._PROCESSING_KEY] = [original]
    with patch("app.queue._get_redis", return_value=redis):
        queue = TaskQueue()
        await queue._recover_processing_tasks()
        assert redis.lists[queue_module._PROCESSING_KEY] == [original]
        await queue._recover_processing_tasks(recover_legacy=True)
        assert redis.lists[queue_module._PROCESSING_KEY] == []
        assert len(redis.lists["gemaibotv2:queue:2"]) == 1


@pytest.mark.asyncio
async def test_expired_owner_cannot_ack_or_retry_recovered_task(sample_task):
    redis = QueueRedis()
    with patch("app.queue._get_redis", return_value=redis):
        first, second = TaskQueue(), TaskQueue()
        first._use_redis = True
        await redis.lpush("gemaibotv2:queue:2", _task_to_json(sample_task).encode())
        task, original = await first._dequeue_task()
        await redis.delete(first._lease_key)
        await second._recover_processing_tasks()
        await first._ack_task(original)
        await first._nack_task(task, original)
        assert len(redis.lists["gemaibotv2:queue:2"]) == 1


@pytest.mark.asyncio
async def test_lease_loss_cancels_redis_handlers_but_preserves_local_work(monkeypatch):
    queue = TaskQueue()
    queue.running = True
    local = asyncio.create_task(asyncio.sleep(60))
    owned = asyncio.create_task(asyncio.sleep(60))
    queue._executions = {"local": local, "owned": owned}
    queue._redis_executions = {"owned"}
    monkeypatch.setattr(queue_module, "_LEASE_RENEW_INTERVAL", 0)
    monkeypatch.setattr(queue_module, "_get_redis", lambda: None)
    try:
        await queue._maintain_lease()
        await asyncio.sleep(0)
        assert queue._lease_lost
        assert owned.cancelled()
        assert not local.done()
        assert not await queue._ensure_lease(QueueRedis())
        queue._redis_executions.clear()
        old_owner = queue._owner_id
        queue.running = False
        assert await queue._ensure_lease(QueueRedis())
        assert queue._owner_id != old_owner
    finally:
        local.cancel()
        owned.cancel()
        await asyncio.gather(local, owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_running_job_stops_handler_and_keeps_worker_available():
    queue = TaskQueue(max_workers=1)
    started = asyncio.Event()
    stopped = asyncio.Event()
    next_done = asyncio.Event()

    async def handler(**data):
        if data.get("next"):
            next_done.set()
            return {}
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    queue._task_handlers["test"] = handler
    worker = asyncio.create_task(queue._worker("test"))
    try:
        task_id = await queue.add_task(42, "test", {})
        await asyncio.wait_for(started.wait(), 1)
        assert await queue.cancel_task(task_id, 42)
        await asyncio.wait_for(stopped.wait(), 1)
        await queue.add_task(42, "test", {"next": True})
        await asyncio.wait_for(next_done.wait(), 1)
        assert queue.tasks[task_id].status == TaskStatus.CANCELLED
    finally:
        worker.cancel()
        await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", [False, True])
async def test_enqueue_failure_local_task_is_consumed_with_existing_redis_client(read_error):
    redis = AsyncMock()
    redis.lpush.side_effect = ConnectionError("enqueue unavailable")
    redis.eval.side_effect = [1, None, None, None, None]
    if read_error:
        redis.eval.side_effect = ConnectionError("dequeue unavailable")

    with patch("app.queue._get_redis", return_value=redis):
        queue = TaskQueue(max_workers=1)
        queue._use_redis = True
        task_id = await queue.add_task(42, "document_processing", {"filename": "local.pdf"})

        task, original_json = await queue._dequeue_task()

        assert task is not None
        assert task.id == task_id
        assert original_json is None  # A local task must not ACK a Redis processing entry.
        assert queue._fallback_queue.empty()


@pytest.mark.asyncio
async def test_queue_size_includes_both_redis_and_local_backlogs():
    redis = AsyncMock()
    redis.lpush.side_effect = ConnectionError("enqueue unavailable")
    redis.llen.return_value = 2
    with patch("app.queue._get_redis", return_value=redis):
        queue = TaskQueue(max_workers=1)
        queue._use_redis = True
        await queue.add_task(42, "document_processing", {"filename": "local.pdf"})

        stats = await queue.get_queue_stats()

        assert stats["queue_size"] == 9
        assert stats["backend"] == "mixed"


@pytest.mark.asyncio
async def test_failed_backlog_read_reports_unknown_instead_of_zero():
    redis = AsyncMock()
    redis.llen.side_effect = ConnectionError("unavailable")
    with patch("app.queue._get_redis", return_value=redis):
        queue = TaskQueue(max_workers=1)
        queue._use_redis = True
        stats = await queue.get_queue_stats()
    assert stats["queue_size"] is None
    assert stats["redis_available"] is False
    assert stats["backend"] == "redis_unavailable"


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


class TestTaskQueueFallback:
    """Test in-memory fallback when Redis unavailable."""

    @pytest.mark.asyncio
    async def test_add_task_without_redis(self):
        """Tasks should be accepted into fallback queue when Redis is None."""
        with patch("app.queue._get_redis", return_value=None):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = False

            task_id = await queue.add_task(
                user_id=1,
                task_type="document_processing",
                data={"filename": "test.pdf"},
            )

            assert task_id != ""
            assert task_id in queue.tasks
            assert queue.tasks[task_id].status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_stats_show_memory_backend(self):
        """Stats should report 'memory' backend when Redis unavailable."""
        with patch("app.queue._get_redis", return_value=None):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = False

            stats = await queue.get_queue_stats()
            assert stats["backend"] == "memory"


class TestTaskQueueRedis:
    """Test Redis-backed queue operations."""

    @pytest.mark.asyncio
    async def test_add_task_to_redis(self):
        """Task should be LPUSH-ed to Redis."""
        mock_redis = QueueRedis()

        with patch("app.queue._get_redis", return_value=mock_redis):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = True

            task_id = await queue.add_task(
                user_id=42,
                task_type="document_processing",
                data={"filename": "test.pdf"},
                priority=TaskPriority.URGENT,
            )

            assert task_id != ""
            saved = mock_redis.lists["gemaibotv2:queue:4"]
            assert len(saved) == 1
            assert _task_from_json(saved[0]).id == task_id

    @pytest.mark.asyncio
    async def test_dequeue_respects_priority(self):
        """Dequeue should check URGENT (4) before LOW (1)."""
        mock_redis = QueueRedis()
        # First 3 priority levels return None, LOW returns a task
        call_count = 0

        async def mock_rpoplpush(src, dst):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return None  # No URGENT, HIGH, NORMAL tasks
            # Return a LOW priority task
            task = Task(
                id="low-task",
                user_id=1,
                task_type="cleanup_metrics",
                data={},
                priority=TaskPriority.LOW,
                status=TaskStatus.PENDING,
                created_at=datetime.now(tz=UTC),
            )
            return _task_to_json(task).encode()

        mock_redis.rpoplpush = AsyncMock(side_effect=mock_rpoplpush)

        with patch("app.queue._get_redis", return_value=mock_redis):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = True

            task, original_json = await queue._dequeue_task()

            assert task is not None
            assert task.id == "low-task"
            assert original_json is not None
            assert call_count == 4  # Checked all 4 priority levels

    @pytest.mark.asyncio
    async def test_crash_recovery(self):
        """Expired owners are recovered, with no duplicate under two recoverers."""
        stuck_task = Task(
            id="stuck-1",
            user_id=99,
            task_type="document_processing",
            data={"filename": "stuck.pdf"},
            priority=TaskPriority.NORMAL,
            status=TaskStatus.RUNNING,
            created_at=datetime.now(tz=UTC),
        )
        stuck_json = _task_to_json(stuck_task).encode()

        mock_redis = QueueRedis()
        mock_redis.sets[queue_module._OWNERS_KEY] = {"crashed"}
        mock_redis.lists[f"{queue_module._PROCESSING_KEY}:crashed"] = [stuck_json]

        with patch("app.queue._get_redis", return_value=mock_redis):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = True
            await queue._recover_processing_tasks()
            await TaskQueue()._recover_processing_tasks()

            # Verify task was re-queued
            assert len(mock_redis.lists["gemaibotv2:queue:2"]) == 1
            assert mock_redis.lists[f"{queue_module._PROCESSING_KEY}:crashed"] == []
            assert not mock_redis.sets[queue_module._OWNERS_KEY]

            # Verify task is in memory cache with incremented retry
            assert "stuck-1" in queue.tasks
            assert queue.tasks["stuck-1"].retry_count == 1

    @pytest.mark.asyncio
    async def test_cancel_task(self):
        """Cancelled tasks should not be processed."""
        with patch("app.queue._get_redis", return_value=None):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = False

            task_id = await queue.add_task(
                user_id=42,
                task_type="document_processing",
                data={"filename": "cancel_me.pdf"},
            )

            result = await queue.cancel_task(task_id, user_id=42)
            assert result is True
            assert queue.tasks[task_id].status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_wrong_user_rejected(self):
        """Only the task owner can cancel."""
        with patch("app.queue._get_redis", return_value=None):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = False

            task_id = await queue.add_task(
                user_id=42,
                task_type="document_processing",
                data={"filename": "test.pdf"},
            )

            result = await queue.cancel_task(task_id, user_id=999)
            assert result is False

    @pytest.mark.asyncio
    async def test_stats_with_redis(self):
        """Stats should report Redis queue sizes."""
        mock_redis = AsyncMock()
        mock_redis.llen = AsyncMock(return_value=2)

        with patch("app.queue._get_redis", return_value=mock_redis):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = True

            stats = await queue.get_queue_stats()
            assert stats["backend"] == "redis"
            assert stats["queue_size"] == 8  # 4 priorities * 2 each

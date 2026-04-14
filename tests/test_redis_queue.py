"""Tests for Redis-backed persistent queue.

Validates:
1. Task serialization/deserialization round-trip.
2. Enqueue/dequeue cycle with mock Redis.
3. Priority ordering (URGENT before LOW).
4. Crash recovery: tasks stuck in processing re-queued on startup.
5. Fallback to in-memory when Redis unavailable.
6. Task cancellation is respected.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.queue import (
    Task,
    TaskPriority,
    TaskQueue,
    TaskStatus,
    _task_from_json,
    _task_to_json,
)


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

    def test_deserialize_from_bytes(self, sample_task):
        json_bytes = _task_to_json(sample_task).encode("utf-8")
        restored = _task_from_json(json_bytes)
        assert restored.id == sample_task.id

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
        mock_redis = AsyncMock()

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
            mock_redis.lpush.assert_awaited_once()
            call_args = mock_redis.lpush.call_args
            assert call_args[0][0] == "gemaibotv2:queue:4"  # URGENT = 4

    @pytest.mark.asyncio
    async def test_dequeue_respects_priority(self):
        """Dequeue should check URGENT (4) before LOW (1)."""
        mock_redis = AsyncMock()
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
        """Tasks stuck in processing list should be re-queued on startup."""
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

        mock_redis = AsyncMock()
        mock_redis.lrange = AsyncMock(return_value=[stuck_json])
        mock_redis.lpush = AsyncMock()
        mock_redis.delete = AsyncMock()

        with patch("app.queue._get_redis", return_value=mock_redis):
            queue = TaskQueue(max_workers=1)
            queue._use_redis = True
            await queue._recover_processing_tasks()

            # Verify task was re-queued
            mock_redis.lpush.assert_awaited_once()
            call_args = mock_redis.lpush.call_args
            assert call_args[0][0] == "gemaibotv2:queue:2"  # NORMAL = 2

            # Verify processing list was cleared
            mock_redis.delete.assert_awaited_once_with("gemaibotv2:processing")

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

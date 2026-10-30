"""Compatibility facade and typed production handler behavior over the v2 store."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.queue import TaskPriority, TaskQueue, TaskStatus
from app.queue_contracts import JobOutcome, JobResult, JobState
from app.queue_store import MemoryQueueStore


@pytest.fixture
def task_queue(monkeypatch):
    @asynccontextmanager
    async def scope():
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", scope)
    queue = TaskQueue(max_workers=2, store=MemoryQueueStore())
    queue.register_handler("test_task", AsyncMock(return_value=JobResult(JobOutcome.SUCCEEDED)), lane="deferred")
    return queue


async def test_start_stop_owns_all_lane_workers(task_queue):
    await task_queue.start()
    assert len(task_queue.workers) == 6
    assert all(not worker.done() for worker in task_queue.workers)
    await task_queue.stop()
    assert not task_queue.running and not task_queue.workers and not task_queue._executions


async def test_legacy_adapter_and_shared_status(task_queue):
    task_id = await task_queue.add_task(0, "test_task", {}, TaskPriority.HIGH)
    assert task_id
    assert (await task_queue.get_task_status(task_id)).status is TaskStatus.PENDING
    await task_queue.start()
    try:
        for _ in range(100):
            task = await task_queue.get_task_status(task_id)
            if task.status is TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)
        assert task.status is TaskStatus.COMPLETED
        assert task.attempts_started == 1
        assert task.data == {}
    finally:
        await task_queue.stop()


async def test_cancel_wrong_user_and_pending_owner(task_queue):
    task_id = await task_queue.add_task(0, "test_task", {})
    assert not await task_queue.cancel_task(task_id, 99)
    assert await task_queue.cancel_task(task_id, 0)
    assert (await task_queue.read_job(task_id)).state is JobState.CANCELLED
    assert await task_queue._choose_store().claim("deferred", "owner") is None


async def test_unknown_kind_is_permanent_rejection(task_queue):
    assert await task_queue.add_task(0, "unknown_kind", {}) == ""
    assert not task_queue._choose_store().jobs


@pytest.mark.asyncio
async def test_handle_document_processing(task_queue):
    # Mock app.document_processor.process_uploaded_document
    with patch("app.document_processor.process_uploaded_document", new_callable=AsyncMock) as mock_process:
        mock_process.return_value = {
            "pages": 5,
            "text_length": 100,
            "paragraphs": 10,
            "tables": 1,
        }

        result = await task_queue._handle_document_processing(file_data=b"test", filename="test.pdf", user_id=1)

        assert result.outcome is JobOutcome.SUCCEEDED
        assert result.result_summary["pages"] == 5

        mock_process.assert_called_once_with(b"test", "test.pdf", 1)


@pytest.mark.asyncio
async def test_handle_document_processing_error(task_queue):
    with patch("app.document_processor.process_uploaded_document", new_callable=AsyncMock) as mock_process:
        mock_process.return_value = {"error": "Processing failed"}

        result = await task_queue._handle_document_processing(file_data=b"test", filename="test.pdf", user_id=1)

        assert result.outcome is JobOutcome.PERMANENT_FAILURE
        assert result.reason_code == "document_processing_failed"


@pytest.mark.asyncio
async def test_handle_cleanup_metrics(task_queue):
    # Mock app.database.db_query
    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:
        result = await task_queue._handle_cleanup_metrics()

        assert result.outcome is JobOutcome.SUCCEEDED
        assert mock_db_query.call_count == 2  # One for metrics, one for error_logs

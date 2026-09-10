import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.observability.context import current_context, request_scope
from app.queue import TaskPriority, TaskQueue, TaskStatus


@pytest.fixture
def task_queue():
    queue = TaskQueue(max_workers=2)
    return queue


@pytest.mark.asyncio
async def test_init(task_queue):
    assert task_queue.max_workers == 2
    assert task_queue._fallback_queue.maxsize == 100
    assert len(task_queue.workers) == 0
    assert not task_queue.running


@pytest.mark.asyncio
async def test_add_task(task_queue):
    with request_scope(request_id="a" * 32, user_id=1, chat_id=2):
        task_id = await task_queue.add_task(
            user_id=1,
            task_type="test_task",
            data={"key": "value"},
            priority=TaskPriority.HIGH,
        )
    assert task_id
    assert task_id in task_queue.tasks
    task = task_queue.tasks[task_id]
    assert task.priority == TaskPriority.HIGH
    assert task.status == TaskStatus.PENDING

    # Check task is in tasks dict (queued to fallback since no Redis)
    assert task.user_id == 1
    assert task.observability_context["trace_id"] == "a" * 32


@pytest.mark.asyncio
async def test_start_stop(task_queue):
    await task_queue.start()
    assert task_queue.running
    assert len(task_queue.workers) == 2
    for w in task_queue.workers:
        assert not w.done()

    await task_queue.stop()
    assert not task_queue.running
    assert len(task_queue.workers) == 0


@pytest.mark.asyncio
async def test_worker_success(task_queue):
    # Mock _execute_task to return a result
    task_queue._execute_task = AsyncMock(return_value={"success": True})
    captured: list[tuple[str, dict[str, object]]] = []

    with (
        patch("app.queue._get_redis", return_value=None),
        patch("app.queue.emit", side_effect=lambda event, **fields: captured.append((event, fields))),
    ):
        await task_queue.start()

        task_id = await task_queue.add_task(user_id=1, task_type="test_task", data={"key": "value"})

        # Wait for task completion (Event-based wakeup should be near-instant)
        for _ in range(20):
            task = await task_queue.get_task_status(task_id)
            if task.status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.1)

        await task_queue.stop()

    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"success": True}
    task_queue._execute_task.assert_called_once()
    terminal = [fields for event, fields in captured if event == "job.finished"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_worker_restores_origin_and_sets_execution_context(task_queue):
    seen = None

    async def execute(_task):
        nonlocal seen
        seen = current_context()
        return {"status": "completed"}

    task_queue._execute_task = AsyncMock(side_effect=execute)
    with patch("app.queue._get_redis", return_value=None):
        await task_queue.start()
        with request_scope(request_id="b" * 32, user_id=7, chat_id=8):
            task_id = await task_queue.add_task(user_id=7, task_type="test_task", data={})
        for _ in range(20):
            if task_queue.tasks[task_id].status == TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.1)
        await task_queue.stop()

    assert seen is not None
    assert seen.trace_id == "b" * 32
    assert seen.task_id == task_id
    assert len(seen.execution_id or "") == 32
    assert seen.operation == "job.execute"


@pytest.mark.asyncio
async def test_worker_failure_retry(task_queue):
    # Mock _execute_task to raise exception
    task_queue._execute_task = AsyncMock(side_effect=Exception("Test Error"))
    captured: list[tuple[str, dict[str, object]]] = []

    with (
        patch("app.queue._get_redis", return_value=None),
        patch("app.queue.emit", side_effect=lambda event, **fields: captured.append((event, fields))),
    ):
        await task_queue.start()

        task_id = await task_queue.add_task(user_id=1, task_type="test_task", data={"key": "value"})

        # Wait for task failure (after retries) — Event wakeup + re-enqueue cycles
        for _ in range(100):  # 10 seconds max
            task = await task_queue.get_task_status(task_id)
            if task.status == TaskStatus.FAILED:
                break
            await asyncio.sleep(0.1)

        await task_queue.stop()

    assert task.status == TaskStatus.FAILED
    assert task.error.startswith("Exception:")
    assert len(task.error.partition(":")[2]) == 32
    assert task.retry_count == task.max_retries
    terminal = [fields for event, fields in captured if event == "job.finished"]
    assert terminal
    assert all(fields["outcome"] == "failed" for fields in terminal)


@pytest.mark.asyncio
async def test_cancel_task(task_queue):
    task_id = await task_queue.add_task(user_id=1, task_type="test_task", data={"key": "value"})

    cancelled = await task_queue.cancel_task(task_id, user_id=1)
    assert cancelled

    task = await task_queue.get_task_status(task_id)
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_wrong_user(task_queue):
    task_id = await task_queue.add_task(user_id=1, task_type="test_task", data={"key": "value"})

    cancelled = await task_queue.cancel_task(task_id, user_id=2)
    assert not cancelled

    task = await task_queue.get_task_status(task_id)
    assert task.status == TaskStatus.PENDING


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

        assert result["status"] == "completed"
        assert result["pages"] == 5

        mock_process.assert_called_once_with(b"test", "test.pdf", 1)


@pytest.mark.asyncio
async def test_handle_document_processing_error(task_queue):
    with patch("app.document_processor.process_uploaded_document", new_callable=AsyncMock) as mock_process:
        mock_process.return_value = {"error": "Processing failed"}

        result = await task_queue._handle_document_processing(file_data=b"test", filename="test.pdf", user_id=1)

        assert result["status"] == "failed"
        assert result["error"] == "Processing failed"


@pytest.mark.asyncio
async def test_handle_cleanup_metrics(task_queue):
    # Mock app.database.db_query
    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:
        result = await task_queue._handle_cleanup_metrics()

        assert result["status"] == "completed"
        assert mock_db_query.call_count == 2  # One for metrics, one for error_logs

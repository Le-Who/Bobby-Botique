import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from app.queue import TaskQueue, TaskPriority, TaskStatus, Task

@pytest.fixture
def task_queue():
    queue = TaskQueue(max_workers=2)
    return queue

@pytest.mark.asyncio
async def test_init(task_queue):
    assert task_queue.max_workers == 2
    assert task_queue.queue.maxsize == 100
    assert len(task_queue.workers) == 0
    assert not task_queue.running

@pytest.mark.asyncio
async def test_add_task(task_queue):
    task_id = await task_queue.add_task(
        user_id=1,
        task_type="test_task",
        data={"key": "value"},
        priority=TaskPriority.HIGH
    )
    assert task_id
    assert task_id in task_queue.tasks
    task = task_queue.tasks[task_id]
    assert task.priority == TaskPriority.HIGH
    assert task.status == TaskStatus.PENDING

    # Check queue content (priority is negative value)
    priority, q_task_id = await task_queue.queue.get()
    assert q_task_id == task_id
    assert priority == -TaskPriority.HIGH.value

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

    await task_queue.start()

    task_id = await task_queue.add_task(
        user_id=1,
        task_type="test_task",
        data={"key": "value"}
    )

    # Wait for task completion
    for _ in range(10):
        task = await task_queue.get_task_status(task_id)
        if task.status == TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.1)

    await task_queue.stop()

    assert task.status == TaskStatus.COMPLETED
    assert task.result == {"success": True}
    task_queue._execute_task.assert_called_once()

@pytest.mark.asyncio
async def test_worker_failure_retry(task_queue):
    # Mock _execute_task to raise exception
    task_queue._execute_task = AsyncMock(side_effect=Exception("Test Error"))

    await task_queue.start()

    task_id = await task_queue.add_task(
        user_id=1,
        task_type="test_task",
        data={"key": "value"}
    )

    # Wait for task failure (after retries)
    for _ in range(50): # 5 seconds max
        task = await task_queue.get_task_status(task_id)
        if task.status == TaskStatus.FAILED:
            break
        await asyncio.sleep(0.1)

    await task_queue.stop()

    assert task.status == TaskStatus.FAILED
    assert task.error == "Test Error"
    assert task.retry_count == task.max_retries

@pytest.mark.asyncio
async def test_cancel_task(task_queue):
    task_id = await task_queue.add_task(
        user_id=1,
        task_type="test_task",
        data={"key": "value"}
    )

    cancelled = await task_queue.cancel_task(task_id, user_id=1)
    assert cancelled

    task = await task_queue.get_task_status(task_id)
    assert task.status == TaskStatus.CANCELLED

@pytest.mark.asyncio
async def test_cancel_task_wrong_user(task_queue):
    task_id = await task_queue.add_task(
        user_id=1,
        task_type="test_task",
        data={"key": "value"}
    )

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
            "tables": 1
        }

        result = await task_queue._handle_document_processing(
            file_data=b"test",
            filename="test.pdf",
            user_id=1
        )

        assert result["status"] == "completed"
        assert result["pages"] == 5

        mock_process.assert_called_once_with(b"test", "test.pdf", 1)

@pytest.mark.asyncio
async def test_handle_document_processing_error(task_queue):
    with patch("app.document_processor.process_uploaded_document", new_callable=AsyncMock) as mock_process:
        mock_process.return_value = {"error": "Processing failed"}

        result = await task_queue._handle_document_processing(
            file_data=b"test",
            filename="test.pdf",
            user_id=1
        )

        assert result["status"] == "failed"
        assert result["error"] == "Processing failed"

@pytest.mark.asyncio
async def test_handle_cleanup_metrics(task_queue):
    # Mock app.database.db_query
    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:
        result = await task_queue._handle_cleanup_metrics()

        assert result["status"] == "completed"
        assert mock_db_query.call_count == 2 # One for metrics, one for error_logs

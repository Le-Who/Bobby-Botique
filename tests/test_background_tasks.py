"""Tests for app.utils.background_tasks — shared task start/cancel utilities."""

import asyncio
import contextlib
import inspect
import logging
from unittest.mock import MagicMock, patch

import pytest

from app.utils.background_tasks import TaskManager, cancel_background_task, start_background_task


@pytest.fixture
def job_events():
    from app.observability.context import current_context

    captured = []

    class CaptureJobs(logging.Handler):
        def emit(self, record):
            event = getattr(record, "_event_name", "")
            if event.startswith("job."):
                captured.append((event, record, current_context()))

    logger = logging.getLogger("app.observability")
    previous_level = logger.level
    handler = CaptureJobs()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield captured
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# ── start_background_task ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_background_task_creates_new_when_none():
    """Creates a new task when task_ref is None."""
    result_holder = []

    async def work():
        result_holder.append("done")

    task = start_background_task(None, work, "test-task")
    assert isinstance(task, asyncio.Task)
    await task
    assert result_holder == ["done"]


@pytest.mark.asyncio
async def test_start_background_task_creates_new_when_done():
    """Creates a new task when the old one has finished."""

    async def noop():
        pass

    old = asyncio.create_task(noop())
    await old  # finish it

    flag = []

    async def work():
        flag.append(1)

    new = start_background_task(old, work, "test-task")
    assert new is not old
    await new
    assert flag == [1]


@pytest.mark.asyncio
async def test_start_background_task_returns_existing_when_running():
    """Returns existing task if it's still running."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        started.set()
        await release.wait()

    existing = asyncio.create_task(slow())
    await started.wait()

    returned = start_background_task(existing, lambda: None, "test-task")
    assert returned is existing

    release.set()
    await existing


# ── cancel_background_task ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_background_task_cancels_running():
    """Cancels a running task and sets attr to None."""
    release = asyncio.Event()

    async def hang():
        await release.wait()

    owner = MagicMock()
    owner._task = asyncio.create_task(hang())

    await cancel_background_task(owner, "_task")

    assert owner._task is None


@pytest.mark.asyncio
async def test_cancel_background_task_noop_when_no_task():
    """No error when attribute doesn't exist."""
    owner = MagicMock(spec=[])  # no attributes
    await cancel_background_task(owner, "_task")
    # Should not raise


@pytest.mark.asyncio
async def test_cancel_background_task_noop_when_none():
    """No error when the attribute is None."""
    owner = MagicMock()
    owner._task = None

    await cancel_background_task(owner, "_task")
    # Should not raise


# ── TaskManager (instance-based) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_taskmanager_submit_retryable():
    tm = TaskManager()  # fresh instance for test isolation

    attempts = 0
    success = False

    async def failing_factory():
        nonlocal attempts, success
        attempts += 1
        if attempts < 3:
            raise ValueError("Intentional failure")
        success = True
        return attempts

    original_sleep = asyncio.sleep

    async def fast_sleep(delay, *args, **kwargs):
        await original_sleep(0.01)

    with patch("asyncio.sleep", side_effect=fast_sleep):
        tm.submit_retryable(failing_factory, retry=3)
        await tm.drain(timeout=2.0)

    assert success is True
    assert attempts == 3


@pytest.mark.asyncio
async def test_taskmanager_emits_canonical_job_lifecycle_once_per_execution():
    tm = TaskManager()
    captured = []
    attempts = 0

    async def retry_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry me")

    async def fast_sleep(_delay):
        return None

    with (
        patch("app.utils.background_tasks.emit", side_effect=lambda event, **fields: captured.append((event, fields))),
        patch("app.utils.background_tasks.asyncio.sleep", side_effect=fast_sleep),
    ):
        tm.submit_retryable(retry_once, retry=1, operation="test.retry")
        await tm.drain(timeout=2.0)

    names = [event for event, _fields in captured]
    assert names == ["job.enqueued", "job.started", "job.retry_scheduled", "job.finished"]
    assert len([event for event in names if event == "job.finished"]) == 1
    assert captured[-1][1]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_taskmanager_capacity_rejection_uses_catalog_event():
    tm = TaskManager()
    tm.MAX_TASKS = 0
    captured = []

    async def work():
        return None

    with patch(
        "app.utils.background_tasks.emit",
        side_effect=lambda event, **fields: captured.append((event, fields)),
    ):
        task = tm.submit(work(), operation="test.capacity")
        await task

    assert [event for event, _fields in captured] == ["job.capacity_rejected"]


@pytest.mark.asyncio
async def test_taskmanager_submit_fire_and_forget():
    tm = TaskManager()  # fresh instance for test isolation

    done = False

    async def work():
        nonlocal done
        done = True

    tm.submit(work())
    await tm.drain(timeout=2.0)

    assert done is True


@pytest.mark.asyncio
async def test_taskmanager_closes_coroutine_cancelled_before_start():
    tm = TaskManager()
    started = False

    async def work():
        nonlocal started
        started = True

    coro = work()
    task = tm.submit(coro)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        assert started is False
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    finally:
        coro.close()


@pytest.mark.asyncio
async def test_taskmanager_does_not_invoke_factory_cancelled_before_start():
    tm = TaskManager()
    invoked = False

    async def work():
        pass

    def factory():
        nonlocal invoked
        invoked = True
        return work()

    task = tm.submit_retryable(factory)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert invoked is False


@pytest.mark.asyncio
async def test_taskmanager_prestart_cancellation_emits_correlated_terminal(job_events):
    from app.observability.context import request_scope

    tm = TaskManager()

    async def work():
        pytest.fail("Cancelled job must not start")

    with request_scope(request_id="a" * 32, user_id=42, chat_id=43):
        task = tm.submit_retryable(work, operation="test.prestart")
        task.cancel()

    with request_scope(request_id="b" * 32, user_id=52, chat_id=53):
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [event for event, _, _ in job_events] == ["job.enqueued", "job.finished"]
    _, enqueued, _ = job_events[0]
    _, finished, context = job_events[-1]
    assert finished.outcome == "cancelled"
    assert finished.attempts == 0
    assert finished.job_id == enqueued.job_id == context.task_id
    assert context.execution_id
    assert context.trace_id == context.request_id == "a" * 32
    assert (context.user_id, context.chat_id) == (42, 43)
    assert context.operation == finished.operation == "test.prestart"


@pytest.mark.asyncio
async def test_taskmanager_backoff_cancellation_emits_one_correlated_terminal(job_events):
    from app.observability.context import request_scope

    tm = TaskManager()
    failed = asyncio.Event()
    invocations = 0

    async def work():
        failed.set()
        raise RuntimeError("retry me")

    def factory():
        nonlocal invocations
        invocations += 1
        return work()

    with request_scope(request_id="c" * 32, user_id=42):
        task = tm.submit_retryable(factory, operation="test.backoff")
    await failed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert invocations == 1
    assert [event for event, _, _ in job_events] == [
        "job.enqueued",
        "job.started",
        "job.attempt_failed",
        "job.retry_scheduled",
        "job.finished",
    ]
    _, started, started_context = job_events[1]
    _, finished, context = job_events[-1]
    assert finished.outcome == "cancelled"
    assert finished.attempts == 1
    assert finished.job_id == started.job_id == context.task_id
    assert context.execution_id == started_context.execution_id
    assert context.trace_id == context.request_id == "c" * 32
    assert context.operation == finished.operation == "test.backoff"


@pytest.mark.asyncio
async def test_taskmanager_failure_callback_cancellation_keeps_failed_terminal(job_events):
    tm = TaskManager()

    async def work():
        raise RuntimeError("job failed")

    def on_error(_error, _description):
        raise asyncio.CancelledError

    tm.register_error_callback(on_error)
    task = tm.submit_retryable(work, retry=0)
    with pytest.raises(asyncio.CancelledError):
        await task

    terminals = [record for event, record, _ in job_events if event == "job.finished"]
    assert len(terminals) == 1
    assert terminals[0].outcome == "failed"


@pytest.mark.asyncio
async def test_taskmanager_adds_named_execution_context():
    from app.observability.context import current_context, request_scope

    tm = TaskManager()
    seen = None

    async def work():
        nonlocal seen
        seen = current_context()

    with request_scope(request_id="c" * 32, user_id=42):
        tm.submit(work(), operation="memory.consolidate", metadata={"source": "test"})
    await tm.drain(timeout=2.0)

    assert seen is not None
    assert seen.trace_id == "c" * 32
    assert seen.operation == "memory.consolidate"
    assert seen.task_id is not None
    assert seen.execution_id is not None


@pytest.mark.asyncio
async def test_taskmanager_drain_awaits_cancelled_task_cleanup(job_events):
    tm = TaskManager()
    running = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def work():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.02)
            cleanup_finished.set()

    task = tm.submit(work())
    await running.wait()

    try:
        drained = await tm.drain(timeout=0, cancel_timeout=0.2)

        assert drained is True
        assert cleanup_started.is_set()
        assert cleanup_finished.is_set()
        assert task.done()
        assert not tm._tasks
        assert [event for event, _, _ in job_events] == ["job.enqueued", "job.started", "job.finished"]
        assert job_events[-1][1].outcome == "cancelled"
        assert job_events[-1][1].attempts == 1
        assert job_events[-1][2].execution_id == job_events[1][2].execution_id
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_taskmanager_rejects_bare_coro_with_retry():
    """Fix 2: bare coroutine + retry > 0 must raise ValueError."""
    tm = TaskManager()

    async def dummy():
        pass

    coro = dummy()
    try:
        with pytest.raises(ValueError, match="Retryable tasks require coro_factory"):
            tm._schedule(coro, coro_factory=None, retry=2)
    finally:
        coro.close()  # Prevent RuntimeWarning: coroutine was never awaited

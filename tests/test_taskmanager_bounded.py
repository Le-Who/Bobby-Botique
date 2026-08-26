import asyncio
import contextlib
import time

import pytest

from app.utils.background_tasks import TaskManager


@pytest.mark.asyncio
async def test_taskmanager_bounded_and_drain():
    """Verify TaskManager limits concurrent tasks and supports draining."""

    tm = TaskManager()  # fresh instance for test isolation
    tm.MAX_TASKS = 3

    try:
        release_event = asyncio.Event()
        started_count = 0

        async def slow_work():
            nonlocal started_count
            started_count += 1
            await release_event.wait()

        # Submit 5 tasks. TaskManager should only accept 3.
        # Track coroutines so we can close rejected ones (avoids RuntimeWarning).
        coroutines = [slow_work() for _ in range(5)]
        for coro in coroutines:
            tm.submit(coro)

        # Yield to allow wrapper tasks to start `slow_work`
        await asyncio.sleep(0.01)

        # Only 3 tasks should actually run
        assert started_count == 3
        # Ensure _tasks set is at the limit
        assert len(tm._tasks) == 3

        # Drain should complete quickly if we release
        drain_task = asyncio.create_task(tm.drain(timeout=2.0))
        await asyncio.sleep(0.01)  # let drain start waiting
        release_event.set()
        await drain_task

        # All tasks should be cleaned up
        assert len(tm._tasks) == 0

        # Explicitly close any rejected coroutines to avoid RuntimeWarning
        for coro in coroutines:
            coro.close()

    finally:
        tm._tasks.clear()


@pytest.mark.asyncio
async def test_taskmanager_drain_reports_cancellation_resistant_task_within_bound(caplog):
    tm = TaskManager()
    running = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cancellation_resistant_work():
        running.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_cleanup.wait()

    task = tm.submit(cancellation_resistant_work())
    await running.wait()

    try:
        started_at = time.monotonic()
        drained = await tm.drain(timeout=0, cancel_timeout=0.02)
        elapsed = time.monotonic() - started_at

        assert drained is False
        assert elapsed < 0.2
        assert not task.done()
        assert "did not finish cancellation cleanup" in caplog.text
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    assert not tm._tasks

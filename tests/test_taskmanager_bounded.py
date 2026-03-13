import asyncio

import pytest

from app.utils.background_tasks import TaskManager


@pytest.mark.asyncio
async def test_taskmanager_bounded_and_drain():
    """Verify TaskManager limits concurrent tasks and supports draining."""

    # Store old max
    old_max = TaskManager.MAX_TASKS
    TaskManager.MAX_TASKS = 3
    TaskManager._tasks.clear()

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
            TaskManager.submit(coro)

        # Yield to allow wrapper tasks to start `slow_work`
        await asyncio.sleep(0.01)

        # Only 3 tasks should actually run
        assert started_count == 3
        # Ensure _tasks set is at the limit
        assert len(TaskManager._tasks) == 3

        # Drain should complete quickly if we release
        drain_task = asyncio.create_task(TaskManager.drain(timeout=2.0))
        await asyncio.sleep(0.01)  # let drain start waiting
        release_event.set()
        await drain_task

        # All tasks should be cleaned up
        assert len(TaskManager._tasks) == 0

        # Explicitly close any rejected coroutines to avoid RuntimeWarning
        for coro in coroutines:
            coro.close()

    finally:
        TaskManager.MAX_TASKS = old_max
        TaskManager._tasks.clear()

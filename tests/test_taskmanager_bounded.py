import asyncio
import contextlib
import time

import pytest

from app.utils.background_tasks import TaskManager


@pytest.mark.asyncio
async def test_taskmanager_bounded_and_drain():
    """Accept only three jobs, then drain all owned work after release."""
    tm = TaskManager()
    tm.MAX_TASKS = 3
    release = asyncio.Event()
    started = asyncio.Event()
    started_count = 0

    async def slow_work():
        nonlocal started_count
        started_count += 1
        if started_count == 3:
            started.set()
        await release.wait()

    coroutines = [slow_work() for _ in range(5)]
    drain_task = None
    try:
        for coro in coroutines:
            tm.submit(coro)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert started_count == 3
        assert len(tm._tasks) == 3
        drain_task = asyncio.create_task(tm.drain(timeout=2))
        release.set()
        assert await drain_task is True
        assert not tm._tasks
    finally:
        release.set()
        owned = tuple(tm._tasks)
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        if drain_task is not None:
            if not drain_task.done():
                drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        for coro in coroutines:
            coro.close()


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

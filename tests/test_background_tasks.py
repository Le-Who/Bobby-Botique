"""Tests for app.utils.background_tasks — shared task start/cancel utilities."""

import asyncio
import pytest
from unittest.mock import MagicMock

from app.utils.background_tasks import start_background_task, cancel_background_task


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

# /app/utils/background_tasks.py
"""Shared background task management utilities.

Provides consistent start/cancel semantics for asyncio background tasks
used across DatabaseManager, TaskQueue, and other components.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any


def start_background_task(
    task_ref: asyncio.Task | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    task_name: str,
) -> asyncio.Task:
    """Start a background task with duplicate-run protection.

    If `task_ref` is still running, returns it unchanged.
    Otherwise creates a new task from ``coro_factory()``.
    """
    if task_ref and not task_ref.done():
        logging.debug("Background task '%s' already running", task_name)
        return task_ref

    return asyncio.create_task(coro_factory())


async def cancel_background_task(owner: object, attr_name: str) -> None:
    """Cancel a background task stored as ``owner.<attr_name>`` and await its shutdown."""
    task = getattr(owner, attr_name, None)
    if not task:
        return

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    setattr(owner, attr_name, None)

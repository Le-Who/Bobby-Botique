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


class TaskManager:
    """Robust background task manager preventing silent failures."""

    MAX_TASKS = 100  # Prevent unbounded background task accumulation
    _tasks: set[asyncio.Task] = set()
    _error_callback: Callable[[Exception, str], Any] | None = None

    @classmethod
    def register_error_callback(cls, callback: Callable[[Exception, str], Any]) -> None:
        """Register a callback to be invoked when a background task exhausts all retries."""
        cls._error_callback = callback

    @classmethod
    def submit(cls, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Run a background task (fire-and-forget, no retries)."""
        return cls._schedule(coro, coro_factory=None, retry=0)

    @classmethod
    def submit_retryable(
        cls,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        retry: int = 3,
    ) -> asyncio.Task:
        """Run a background task with retry capabilities.
        
        The factory must return a fresh coroutine on each call.
        """
        return cls._schedule(None, coro_factory=factory, retry=retry)

    @classmethod
    def _schedule(
        cls,
        coro: Coroutine[Any, Any, Any] | None,
        *,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None,
        retry: int,
    ) -> asyncio.Task:
        # Determine task name for logging
        name_source = coro_factory if coro_factory else coro
        coro_name = getattr(name_source, "__name__", getattr(name_source, "__qualname__", str(name_source)))

        if len(cls._tasks) >= cls.MAX_TASKS:
            logging.warning(
                "TaskManager at capacity (%d). Rejecting task %s",
                cls.MAX_TASKS,
                coro_name,
            )

            async def _noop():
                pass

            return asyncio.create_task(_noop())

        async def _wrapper():
            attempts = 0
            while attempts <= retry:
                try:
                    target = coro_factory() if coro_factory else coro
                    # Note: bare coroutines can only be awaited once, but retry is 0 for them.
                    await target  # type: ignore[misc]
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    attempts += 1
                    logging.error(
                        "Background task %s failed (attempt %d/%d): %s",
                        coro_name,
                        attempts,
                        retry + 1,
                        e,
                        exc_info=True,
                    )
                    if attempts <= retry:
                        await asyncio.sleep(2**attempts)  # Exponential backoff
                    else:
                        if cls._error_callback:
                            try:
                                res = cls._error_callback(e, f"Task {coro_name}")
                                # Execute asynchronously if it's a coroutine
                                if asyncio.iscoroutine(res):
                                    cb_task = asyncio.create_task(res)
                                    cls._tasks.add(cb_task)
                                    cb_task.add_done_callback(cls._tasks.discard)
                            except Exception as cb_err:
                                logging.error("TaskManager error callback failed: %s", cb_err)

        task = asyncio.create_task(_wrapper())
        cls._tasks.add(task)
        task.add_done_callback(cls._tasks.discard)
        return task

    @classmethod
    async def drain(cls, timeout: float = 10.0) -> None:
        """Await all running tasks with a timeout for graceful shutdown."""
        if not cls._tasks:
            return

        logging.info("Draining %d background tasks...", len(cls._tasks))
        # wait doesn't cancel, it just waits up to timeout
        _done, pending = await asyncio.wait(cls._tasks, timeout=timeout)

        if pending:
            logging.warning("Timeout draining background tasks. Cancelling %d tasks.", len(pending))
            for task in pending:
                task.cancel()

            # Allow cancelled tasks a tick to clean up
            await asyncio.sleep(0.1)


# Global helpers
submit_task = TaskManager.submit
submit_retryable = TaskManager.submit_retryable

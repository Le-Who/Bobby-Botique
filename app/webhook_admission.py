"""Own serialized webhook claims through queue handoff and lifecycle stop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class AdmissionClosed(Exception):
    """The application is stopping and an unclaimed update should be retried."""


async def _await_owned[T](child: asyncio.Task[T]) -> T:
    """Observe the same child to completion before restoring caller cancellation."""
    owner = asyncio.current_task()
    cancellation: asyncio.CancelledError | None = None
    while not child.done():
        try:
            await asyncio.shield(child)
        except asyncio.CancelledError as error:
            # A cancelled child also raises through shield. Only owner cancellation
            # is deferred; a completed self-cancelled child must leave this loop.
            if cancellation is None and (not child.done() or (owner is not None and owner.cancelling())):
                cancellation = error
        except BaseException:
            # Retrieve the child's result below, including its diagnostic cause.
            break

    try:
        result = child.result()
    except BaseException as error:
        if cancellation is None:
            raise
        if not isinstance(error, asyncio.CancelledError):
            logger.error(
                "Owned webhook operation failed during caller cancellation",
                exc_info=(type(error), error, error.__traceback__),
            )
        raise cancellation from error
    if cancellation is not None:
        raise cancellation
    return result


class WebhookAdmissionGate:
    """Keep the admission lock until its owned claim-to-queue operation finishes."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    def close(self) -> None:
        """Reject new work before lifecycle cleanup first yields to other resources."""
        self._closed = True

    async def admit[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        if self._closed:
            raise AdmissionClosed
        # Waiting requests remain cancellable and have no retained admission child.
        async with self._lock:
            if self._closed:
                raise AdmissionClosed

            async def run() -> T:
                return await operation()

            child = asyncio.create_task(run(), name="webhook-admission")
            return await _await_owned(child)

    async def shutdown(self, stop_callback: Callable[[], Awaitable[None]]) -> None:
        # Closure and shutdown registration happen synchronously before any await.
        self.close()
        if self._shutdown_task is None:

            async def drain_and_stop() -> None:
                async with self._lock:
                    await stop_callback()

            self._shutdown_task = asyncio.create_task(drain_and_stop(), name="webhook-admission-shutdown")
        await _await_owned(self._shutdown_task)

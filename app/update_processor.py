"""Update processing that preserves PTB's per-user state invariants."""

import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import BaseUpdateProcessor


@dataclass(slots=True)
class _UserLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


def _close_unstarted_coroutine(coroutine: Awaitable[Any]) -> None:
    """Release an owned coroutine when cancellation happens before it starts."""
    if inspect.iscoroutine(coroutine) and inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
        coroutine.close()


class UserScopedUpdateProcessor(BaseUpdateProcessor):
    """Run different users concurrently while serializing each user's updates."""

    __slots__ = ("_user_locks",)

    def __init__(self, max_concurrent_updates: int) -> None:
        super().__init__(max_concurrent_updates)
        self._user_locks: dict[int, _UserLock] = {}

    async def process_update(  # type: ignore[misc,override]
        self,
        update: object,
        coroutine: Awaitable[Any],
    ) -> None:
        """Acquire the user lane before PTB's global concurrency semaphore.

        PTB's base implementation intentionally acquires the global semaphore before
        calling ``do_process_update``. Doing user serialization only in that hook lets
        one user's backlog occupy every global slot. The base method is called after
        admission to the user lane so its limit, cancellation, and error propagation
        semantics remain intact without head-of-line blocking other users.
        """
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        if user_id is None:
            try:
                await super().process_update(update, coroutine)
            finally:
                _close_unstarted_coroutine(coroutine)
            return

        entry = self._user_locks.get(user_id)
        if entry is None:
            entry = _UserLock()
            self._user_locks[user_id] = entry
        entry.references += 1

        try:
            async with entry.lock:
                await super().process_update(update, coroutine)
        finally:
            entry.references -= 1
            if entry.references == 0:
                self._user_locks.pop(user_id, None)
            _close_unstarted_coroutine(coroutine)

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:  # noqa: ARG002
        await coroutine

    async def initialize(self) -> None:
        """No external resources are required."""

    async def shutdown(self) -> None:
        """No external resources are retained between processed updates."""

"""Update processing that preserves PTB's per-user state invariants."""

import asyncio
import inspect
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from telegram.ext import BaseUpdateProcessor

from app.observability.context import request_scope
from app.observability.events import emit, record_exception
from app.observability.ingress import webhook_origins


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
        chat = getattr(update, "effective_chat", None)
        user_id = getattr(user, "id", None)
        chat_id = getattr(chat, "id", None)
        update_id = getattr(update, "update_id", None)
        origin = webhook_origins.consume(update_id) if isinstance(update_id, int) else None
        origin_trace_id = origin.get("trace_id") if origin else None
        origin_span_id = origin.get("span_id") if origin else None
        update_kind = self._update_kind(update)
        started_ns = time.monotonic_ns()
        lock_wait_ms = 0.0
        outcome = "succeeded"
        error_id: str | None = None

        with request_scope(
            user_id=user_id if isinstance(user_id, int) else None,
            chat_id=chat_id if isinstance(chat_id, int) else None,
            operation="telegram.update",
            trace_id=origin_trace_id if isinstance(origin_trace_id, str) else None,
            parent_span_id=origin_span_id if isinstance(origin_span_id, str) else None,
        ):
            emit(
                "telegram.update_started",
                operation="telegram.update",
                update_id=update_id if isinstance(update_id, int) else None,
                update_kind=update_kind,
                transport_origin="webhook" if origin else "polling_or_unlinked",
            )
            try:
                if user_id is None:
                    await super().process_update(update, coroutine)
                else:
                    entry = self._user_locks.get(user_id)
                    if entry is None:
                        entry = _UserLock()
                        self._user_locks[user_id] = entry
                    entry.references += 1
                    lock_started_ns = time.monotonic_ns()
                    try:
                        async with entry.lock:
                            lock_wait_ms = (time.monotonic_ns() - lock_started_ns) / 1_000_000
                            await super().process_update(update, coroutine)
                    finally:
                        entry.references -= 1
                        if entry.references == 0:
                            self._user_locks.pop(user_id, None)
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception as error:
                outcome = "failed"
                error_id = record_exception(
                    "telegram.update_failed",
                    error,
                    operation="telegram.update",
                    fields={
                        "update_id": update_id if isinstance(update_id, int) else None,
                        "update_kind": update_kind,
                    },
                )
                raise
            finally:
                _close_unstarted_coroutine(coroutine)
                emit(
                    "telegram.update_finished",
                    level="warning" if outcome != "succeeded" else "info",
                    operation="telegram.update",
                    outcome=outcome,
                    update_id=update_id if isinstance(update_id, int) else None,
                    update_kind=update_kind,
                    duration_ms=round((time.monotonic_ns() - started_ns) / 1_000_000, 2),
                    user_lock_wait_ms=round(lock_wait_ms, 2),
                    error_id=error_id,
                )

    @staticmethod
    def _update_kind(update: object) -> str:
        for name in (
            "callback_query",
            "inline_query",
            "chosen_inline_result",
            "edited_message",
            "message",
        ):
            if getattr(update, name, None) is not None:
                return name
        return type(update).__name__.casefold()

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:  # noqa: ARG002
        await coroutine

    async def initialize(self) -> None:
        """No external resources are required."""

    async def shutdown(self) -> None:
        """No external resources are retained between processed updates."""

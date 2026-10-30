"""Capacity accounting for accepted Telegram updates."""

import asyncio
import time
from collections import deque


class OutstandingUpdateQueue[T](asyncio.Queue[T]):
    """Bound accepted updates until their consumer calls ``task_done``.

    PTB fetches updates before waiting for its processing slots. ``qsize`` and
    ``buffered`` count only queued items; ``outstanding`` also includes those
    fetched updates. Getting an item therefore never frees admission capacity.
    The ordinary ``put`` contract also applies to PTB's shutdown sentinel: it
    waits for a completed update, then joins the same FIFO and completion count.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("Update capacity must be positive")
        super().__init__(maxsize=capacity)
        self._outstanding = 0
        self._capacity_waiters: deque[asyncio.Future[None]] = deque()

    @property
    def outstanding(self) -> int:
        """Accepted updates that have not received terminal ``task_done``."""
        return self._outstanding

    @property
    def buffered(self) -> int:
        """Updates still waiting for the PTB fetcher."""
        return self.qsize()

    def full(self) -> bool:
        return self._outstanding >= self.maxsize

    def metrics_snapshot(self) -> dict[str, int | float | str | bool]:
        return {
            "scope": "process",
            "available": True,
            "observed_at": time.time(),
            "buffered": self.buffered,
            "outstanding": self.outstanding,
            "capacity": self.maxsize,
        }

    async def put(self, item: T) -> None:
        while self.full():
            waiter = asyncio.get_running_loop().create_future()
            self._capacity_waiters.append(waiter)
            try:
                await waiter
            except BaseException:
                waiter.cancel()
                # A completion may already have removed and woken this waiter.
                if waiter in self._capacity_waiters:
                    self._capacity_waiters.remove(waiter)
                if not self.full():
                    self._wake_producer()
                raise
        self.put_nowait(item)

    def put_nowait(self, item: T) -> None:
        super().put_nowait(item)
        self._outstanding += 1
        update_id = getattr(item, "update_id", None)
        if isinstance(update_id, int) and not isinstance(update_id, bool):
            from app.observability.ingress import update_admission_times

            update_admission_times.remember(update_id)

    def task_done(self) -> None:
        # The base queue detects repeated task_done and owns join's event.
        super().task_done()
        self._outstanding -= 1
        self._wake_producer()

    def _wake_producer(self) -> None:
        while self._capacity_waiters:
            waiter = self._capacity_waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                break

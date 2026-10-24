"""Pace queued PCM so Live VAD receives speech instead of a network burst."""

from asyncio import sleep
from collections.abc import Awaitable, Callable
from time import monotonic


class LiveAudioPacer:
    """Leave realtime packets alone; drain a backlog at at most 2x audio speed."""

    def __init__(self) -> None:
        self._next_send = 0.0

    async def pace(
        self,
        byte_count: int,
        *,
        sample_rate: int = 16000,
        wait_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        now = monotonic()
        if self._next_send > now:
            await sleep(self._next_send - now)
        if wait_ready is not None:
            await wait_ready()
        # Input is signed PCM16 mono: two bytes per sample. A two-times
        # catch-up rate drains queued silence without an ever-growing delay.
        self._next_send = max(self._next_send, monotonic()) + byte_count / (sample_rate * 2 * 2)

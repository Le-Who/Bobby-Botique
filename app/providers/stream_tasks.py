"""Keep request-owned provider finalizers alive through owner cancellation."""

import asyncio
from collections.abc import Iterable
from typing import Any


async def drain_provider_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Await every child without forwarding repeated cancellation into cleanup.

    Callers cancel their unfinished children once before entering this drain.
    A later cancellation of the owner is propagated after the children settle.
    """
    completion = asyncio.gather(*tasks, return_exceptions=True)
    cancellation: asyncio.CancelledError | None = None
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    completion.result()
    if cancellation is not None:
        raise cancellation

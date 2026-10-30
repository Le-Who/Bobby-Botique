"""Cancellation checkpoint shared by Daily provider and persistence boundaries."""

import asyncio


async def checkpoint() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
    from app.queue import current_job_guard

    guard = current_job_guard()
    if guard is not None:
        await guard.checkpoint()

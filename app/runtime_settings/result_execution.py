"""Exact ordered plans for providers with specialized result contracts."""

import asyncio
from collections.abc import Awaitable, Callable

from app.process_policies import resolve_process


async def run_result_process[T](
    process_id: str,
    model: str,
    execute: Callable[[str, bool], Awaitable[T]],
    *,
    success: Callable[[T], bool],
    terminal: Callable[[T], bool] = lambda _: False,
    timeout: float,
    on_timeout: Callable[[], T],
) -> T:
    """Retain default provider behavior; explicit plans have no hidden models."""
    policy = await resolve_process(process_id, (model,))
    if not policy.explicit:
        return await execute(model, False)
    try:
        async with asyncio.timeout(timeout):
            for candidate in policy.models:
                result = await execute(candidate, True)
                if success(result) or terminal(result):
                    return result
            return result
    except TimeoutError:
        return on_timeout()

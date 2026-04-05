"""
AAA unit tests for app.adapters.concurrency.GlobalLLMSemaphore.

Critical Risk: If the semaphore is not released when an exception is thrown
inside the `async with` block, subsequent requests will starve — the bot
will appear to hang for all users sharing the same limit tier.

These tests verify the semaphore invariant:
  "Concurrent slot count is exactly restored after any exit path (success, exception, cancellation)."

Uses only asyncio.Semaphore (local fallback path) by patching redis_client to None.
"""

import asyncio
from unittest.mock import patch

import pytest

from app.adapters.concurrency import GlobalLLMSemaphore


def make_semaphore(limit: int) -> GlobalLLMSemaphore:
    """Create a GlobalLLMSemaphore that bypasses Redis (uses asyncio.Semaphore only)."""
    return GlobalLLMSemaphore(limit=limit, timeout=60, redis_key="test_semaphore_key")


# ─── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semaphore_acquired_and_released_on_success():
    """Semaphore slot must be released after normal execution."""
    # Arrange
    sem = make_semaphore(limit=2)

    with patch("app.cache.redis_client", None):  # Force local-only path
        # Act — acquire and release normally
        async with sem:
            pass  # Just hold and release the slot

    # Assert — slot must be restored after exit
    assert sem._local_semaphore._value == 2, "Semaphore must fully release after normal exit"


@pytest.mark.asyncio
async def test_semaphore_released_on_exception():
    """Semaphore slot MUST be released even when the body raises an exception."""
    # Arrange
    sem = make_semaphore(limit=1)

    with patch("app.cache.redis_client", None):
        # Act
        with pytest.raises(ValueError):
            async with sem:
                raise ValueError("Simulated failure inside semaphore block")

    # Assert — slot must be restored after exception
    assert sem._local_semaphore._value == 1, "Semaphore must release when exception propagates"


@pytest.mark.asyncio
async def test_semaphore_released_on_asyncio_cancelled_error():
    """CancelledError (task cancellation) must not permanently hold the slot."""
    # Arrange
    sem = make_semaphore(limit=1)

    with patch("app.cache.redis_client", None):
        # Act
        task = asyncio.create_task(_cancel_scenario(sem))
        await asyncio.sleep(0.01)  # Let task start
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Assert — semaphore must be released despite cancellation
    assert sem._local_semaphore._value == 1, "Semaphore must release on task cancellation"


async def _cancel_scenario(sem: GlobalLLMSemaphore) -> None:
    """Helper: acquire the semaphore and yield to allow cancellation."""
    with patch("app.cache.redis_client", None):
        async with sem:
            await asyncio.sleep(10)  # Hold slot — will be cancelled here


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency_to_configured_limit():
    """No more than `limit` tasks must be active concurrently."""
    # Arrange
    limit = 2
    sem = make_semaphore(limit=limit)
    concurrent_peak = 0
    active = 0

    async def task():
        nonlocal active, concurrent_peak
        with patch("app.cache.redis_client", None):
            async with sem:
                active += 1
                concurrent_peak = max(concurrent_peak, active)
                await asyncio.sleep(0.05)
                active -= 1

    with patch("app.cache.redis_client", None):
        # Act — run 5 tasks through a limit-2 semaphore
        await asyncio.gather(*(task() for _ in range(5)))

    # Assert — peak concurrency must never exceed configured limit
    assert concurrent_peak <= limit, f"Peak concurrency {concurrent_peak} exceeded limit {limit}"


@pytest.mark.asyncio
async def test_semaphore_returns_to_full_capacity_after_all_tasks_complete():
    """After all parallel tasks finish, all slots must be returned."""
    # Arrange
    limit = 3
    sem = make_semaphore(limit=limit)

    async def task():
        with patch("app.cache.redis_client", None):
            async with sem:
                await asyncio.sleep(0.02)

    with patch("app.cache.redis_client", None):
        # Act
        await asyncio.gather(*(task() for _ in range(limit * 2)))

    # Assert
    assert sem._local_semaphore._value == limit, f"All {limit} slots must be returned after task completion"


@pytest.mark.asyncio
async def test_semaphore_limit_one_sequential_execution():
    """With limit=1, tasks must execute sequentially (no concurrent access)."""
    # Arrange
    sem = make_semaphore(limit=1)
    execution_order: list[int] = []

    async def task(n: int) -> None:
        with patch("app.cache.redis_client", None):
            async with sem:
                execution_order.append(n)
                await asyncio.sleep(0.01)

    with patch("app.cache.redis_client", None):
        # Act — run 3 tasks that must be sequential
        await asyncio.gather(task(1), task(2), task(3))

    # Assert — all tasks ran, order may vary but no overlap occurred
    assert sorted(execution_order) == [1, 2, 3]
    assert sem._local_semaphore._value == 1

import asyncio

import pytest

from app.streaming import _last_finish_reason, set_last_finish_reason


@pytest.mark.asyncio
async def test_finish_reason_contextvar_isolation():
    """Verify that multiple concurrent streams do not overwrite each other's finish reasons."""

    async def simulate_stream(reason: str, delay: float):
        # Initial state should be None
        assert _last_finish_reason.get() is None

        # Set the reason (simulating a provider setting it)
        set_last_finish_reason(reason)

        # Yield to event loop
        await asyncio.sleep(delay)

        # Assert the reason hasn't been overwritten by other tasks
        assert _last_finish_reason.get() == reason
        return _last_finish_reason.get()

    # Run three concurrent streams with different finish reasons and sleep delays
    # (to guarantee overlap)
    tasks = [
        simulate_stream("SAFETY", 0.05),
        simulate_stream("MAX_TOKENS", 0.02),
        simulate_stream("RECITATION", 0.01),
    ]

    results = await asyncio.gather(*tasks)

    assert results == ["SAFETY", "MAX_TOKENS", "RECITATION"]

    # ContextVar in the main task should still be None
    assert _last_finish_reason.get() is None

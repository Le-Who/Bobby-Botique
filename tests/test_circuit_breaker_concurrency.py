import asyncio

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_concurrency():
    """All five calls enter before any may finish; the breaker owns no request-wide lock."""
    cb = CircuitBreaker("ConcurrencyTest", CircuitBreakerConfig(failure_threshold=3, expected_exception=(ValueError,)))
    all_entered = asyncio.Event()
    release = asyncio.Event()
    in_flight = 0
    peak = 0

    async def slow_func():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if in_flight == 5:
            all_entered.set()
        try:
            await release.wait()
            return "success"
        finally:
            in_flight -= 1

    tasks = [asyncio.create_task(cb.call(slow_func)) for _ in range(5)]
    try:
        await asyncio.wait_for(all_entered.wait(), timeout=2)
        assert peak == 5
        release.set()
        assert await asyncio.gather(*tasks) == ["success"] * 5
        assert cb._state == CircuitState.CLOSED
        assert cb._total_requests == 5
        assert cb._total_successes == 5
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cb.shutdown()
    assert cb._monitor_task is None or cb._monitor_task.done()

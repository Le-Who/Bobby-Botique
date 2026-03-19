import asyncio
import time

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_concurrency():
    """Verify that the circuit breaker lock is split and correctly permits concurrent executions."""

    cb = CircuitBreaker(
        "ConcurrencyTest",
        CircuitBreakerConfig(failure_threshold=3, expected_exception=(ValueError,)),
    )

    in_flight = 0
    max_in_flight = 0

    async def slow_func(delay: float, fail: bool = False):
        nonlocal in_flight, max_in_flight

        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)

        # Sleep allows context switch. If the CB lock wraps the whole function,
        # max_in_flight will never exceed 1.
        await asyncio.sleep(delay)

        in_flight -= 1

        if fail:
            raise ValueError("Simulated failure")
        return "success"

    start_time = time.perf_counter()

    # Launch 5 concurrent valid requests (they should run in parallel)
    tasks = [cb.call(slow_func, 0.1, fail=False) for _ in range(5)]
    _results = await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - start_time

    # 5 tasks taking 0.1s in true parallel should take ~0.1s total, not 0.5s
    assert elapsed < 0.3

    # The true test of parallel execution under the CB:
    assert max_in_flight == 5
    assert cb._state == CircuitState.CLOSED
    assert cb._total_requests == 5
    assert cb._total_successes == 5

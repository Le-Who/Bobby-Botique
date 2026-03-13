import asyncio
from unittest.mock import AsyncMock

import pytest

from app.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
)


@pytest.fixture
def config():
    return CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout=0.1,  # Short timeout for testing
        monitor_interval=0.1,
        expected_exception=ValueError,
    )


@pytest.fixture
def mock_time(monkeypatch):
    class MockTimeManager:
        def __init__(self):
            self.current = 1000.0

        def __call__(self):
            return self.current

        def advance(self, seconds: float):
            self.current += seconds

    m = MockTimeManager()
    # Mock time.time specifically in the circuit_breaker module
    monkeypatch.setattr("app.circuit_breaker.time.time", m)
    return m


@pytest.fixture
async def cb(config, mock_time):
    cb = CircuitBreaker("test_cb", config)
    yield cb
    await cb.shutdown()


@pytest.mark.asyncio
async def test_initial_state(cb):
    assert cb.get_state() == CircuitState.CLOSED
    stats = cb.get_stats()
    assert stats["failure_count"] == 0
    assert stats["total_requests"] == 0
    assert stats["state"] == "closed"


@pytest.mark.asyncio
async def test_successful_call(cb):
    mock_func = AsyncMock(return_value="success")
    result = await cb.call(mock_func, "arg1", key="value")

    assert result == "success"
    mock_func.assert_called_once_with("arg1", key="value")
    assert cb.get_state() == CircuitState.CLOSED
    stats = cb.get_stats()
    assert stats["total_successes"] == 1
    assert stats["total_requests"] == 1
    assert stats["failure_count"] == 0


@pytest.mark.asyncio
async def test_failure_counting(cb):
    # Setup mock to fail with expected exception
    mock_func = AsyncMock(side_effect=ValueError("fail"))

    with pytest.raises(ValueError, match="fail"):
        await cb.call(mock_func)

    assert cb.get_stats()["failure_count"] == 1
    assert cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_opens_on_threshold(cb):
    # Threshold is 2
    mock_func = AsyncMock(side_effect=ValueError("fail"))

    # First failure
    with pytest.raises(ValueError):
        await cb.call(mock_func)
    assert cb.get_state() == CircuitState.CLOSED
    assert cb.get_stats()["failure_count"] == 1

    # Second failure -> Should open circuit
    with pytest.raises(ValueError):
        await cb.call(mock_func)
    assert cb.get_state() == CircuitState.OPEN
    assert cb.get_stats()["failure_count"] == 2


@pytest.mark.asyncio
async def test_open_circuit_rejects_calls(cb):
    # Force open state
    await cb.force_open()

    mock_func = AsyncMock()
    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        await cb.call(mock_func)

    assert "is OPEN" in str(excinfo.value)
    mock_func.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_exception_ignored(cb):
    # Exception not in expected_exception (ValueError)
    mock_func = AsyncMock(side_effect=RuntimeError("unexpected"))

    with pytest.raises(RuntimeError):
        await cb.call(mock_func)

    # Should not count as failure
    assert cb.get_stats()["failure_count"] == 0
    assert cb.get_state() == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_recovery_flow_success(cb, mock_time):
    # Setup: Force open
    await cb.force_open()
    assert cb.get_state() == CircuitState.OPEN

    # Advance time past recovery timeout
    mock_time.advance(0.15)

    # Next call should be allowed (HALF_OPEN) and succeed
    mock_func = AsyncMock(return_value="recovered")
    result = await cb.call(mock_func)

    assert result == "recovered"
    assert cb.get_state() == CircuitState.CLOSED
    assert cb.get_stats()["failure_count"] == 0


@pytest.mark.asyncio
async def test_recovery_flow_failure(cb, mock_time):
    # Setup: Force open
    await cb.force_open()

    # Advance time past recovery timeout
    mock_time.advance(0.15)

    # Call fails
    mock_func = AsyncMock(side_effect=ValueError("fail again"))

    with pytest.raises(ValueError):
        await cb.call(mock_func)

    # Should reopen immediately
    assert cb.get_state() == CircuitState.OPEN
    # Failure count increases
    assert cb.get_stats()["failure_count"] > 0


@pytest.mark.asyncio
async def test_manual_control(cb):
    await cb.force_open()
    assert cb.get_state() == CircuitState.OPEN

    await cb.force_close()
    assert cb.get_state() == CircuitState.CLOSED
    assert cb.get_stats()["failure_count"] == 0

    await cb.reset()
    assert cb.get_state() == CircuitState.CLOSED
    assert cb.get_stats()["total_requests"] == 0


@pytest.mark.asyncio
async def test_monitoring_task(cb):
    # Allow monitoring loop to run
    await asyncio.sleep(0.15)

    # We can check logs if we capture them, or just ensure no crash
    # Just basic liveness check
    assert cb.get_state() == CircuitState.CLOSED
    # Stats should be retrievable
    stats = cb.get_stats()
    assert isinstance(stats, dict)


@pytest.mark.asyncio
async def test_get_circuit_breaker_and_shutdown_all():
    from app.circuit_breaker import _circuit_breakers, get_circuit_breaker, shutdown_all_circuit_breakers

    _circuit_breakers.clear()
    cb1 = get_circuit_breaker("test_global_1")
    cb2 = get_circuit_breaker("test_global_1")

    # Needs to return the same instance
    assert cb1 is cb2

    cb3 = get_circuit_breaker("test_global_2")
    assert cb3 is not cb1

    await shutdown_all_circuit_breakers()
    assert len(_circuit_breakers) == 0


@pytest.mark.asyncio
async def test_monitor_loop_max_failures_cap(cb, mock_time):
    # Overfill failures
    cb._failure_count = cb.config.max_failures + 10

    # The background monitor task sleeps for monitor_interval. Let it run once.
    await asyncio.sleep(0.15)

    # Assuming monitor loop ran, it should cap it
    assert cb._failure_count <= cb.config.max_failures


@pytest.mark.asyncio
async def test_should_attempt_reset_not_open(cb):
    # State is CLOSED, should return False
    assert cb._should_attempt_reset() is False

    await cb._set_state(CircuitState.HALF_OPEN)
    assert cb._should_attempt_reset() is False


def test_init_without_running_loop():
    # Attempt to init without a running loop
    from unittest.mock import patch

    import app.circuit_breaker

    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        cb = app.circuit_breaker.CircuitBreaker("test_no_loop")
        assert cb._monitor_task is None


@pytest.mark.asyncio
async def test_start_monitoring_already_done(cb):
    # Attempt to call start_monitoring when already running
    task = cb._monitor_task
    cb._start_monitoring()
    assert task is cb._monitor_task


@pytest.mark.asyncio
async def test_monitor_loop_exception_handling(cb):
    from unittest.mock import patch

    with patch("asyncio.sleep", side_effect=Exception("mocked error")):
        # The monitor loop should catch the exception and log it, then next iteration will fail again if mocked,
        # but here we just want to ensure it handles one exception. Since it's a while True, if sleep always raises,
        # it might infinite loop. Let's just side_effect a single exception then CancelledError.
        with patch("app.circuit_breaker.asyncio.sleep", side_effect=[Exception("mocked"), asyncio.CancelledError()]):
            await cb._monitor_loop()

    # Should exit gracefully on CancelledError
    assert True

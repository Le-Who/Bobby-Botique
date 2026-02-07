import pytest
import asyncio
import time
from unittest.mock import Mock, patch, AsyncMock
from app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState, CircuitBreakerOpenError

@pytest.fixture
def config():
    return CircuitBreakerConfig(
        failure_threshold=2,
        recovery_timeout=0.1,  # Short timeout for testing
        monitor_interval=0.1,
        expected_exception=ValueError
    )

@pytest.fixture
async def cb(config):
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
    assert cb._total_successes == 1
    assert cb._total_requests == 1
    assert cb._failure_count == 0

@pytest.mark.asyncio
async def test_failure_counting(cb):
    # Setup mock to fail with expected exception
    mock_func = AsyncMock(side_effect=ValueError("fail"))

    with pytest.raises(ValueError, match="fail"):
        await cb.call(mock_func)

    assert cb._failure_count == 1
    assert cb.get_state() == CircuitState.CLOSED

@pytest.mark.asyncio
async def test_circuit_opens_on_threshold(cb):
    # Threshold is 2
    mock_func = AsyncMock(side_effect=ValueError("fail"))

    # First failure
    with pytest.raises(ValueError):
        await cb.call(mock_func)
    assert cb.get_state() == CircuitState.CLOSED
    assert cb._failure_count == 1

    # Second failure -> Should open circuit
    with pytest.raises(ValueError):
        await cb.call(mock_func)
    assert cb.get_state() == CircuitState.OPEN
    assert cb._failure_count == 2

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
    assert cb._failure_count == 0
    assert cb.get_state() == CircuitState.CLOSED

@pytest.mark.asyncio
async def test_recovery_flow_success(cb):
    # Setup: Force open
    await cb.force_open()
    assert cb.get_state() == CircuitState.OPEN

    # Wait for recovery timeout (0.1s)
    await asyncio.sleep(0.15)

    # Next call should be allowed (HALF_OPEN) and succeed
    mock_func = AsyncMock(return_value="recovered")
    result = await cb.call(mock_func)

    assert result == "recovered"
    assert cb.get_state() == CircuitState.CLOSED
    assert cb._failure_count == 0

@pytest.mark.asyncio
async def test_recovery_flow_failure(cb):
    # Setup: Force open
    await cb.force_open()

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # Call fails
    mock_func = AsyncMock(side_effect=ValueError("fail again"))

    with pytest.raises(ValueError):
        await cb.call(mock_func)

    # Should reopen immediately
    assert cb.get_state() == CircuitState.OPEN
    # Failure count increases
    assert cb._failure_count > 0

@pytest.mark.asyncio
async def test_manual_control(cb):
    await cb.force_open()
    assert cb.get_state() == CircuitState.OPEN

    await cb.force_close()
    assert cb.get_state() == CircuitState.CLOSED
    assert cb._failure_count == 0

    await cb.reset()
    assert cb.get_state() == CircuitState.CLOSED
    assert cb._total_requests == 0

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

"""Recovery probes and obsolete requests must retain separate state ownership."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError, CircuitState


class AdmissionRejected(RuntimeError):
    pass


@pytest.fixture
async def breaker():
    instance = CircuitBreaker(
        "recovery-ownership",
        CircuitBreakerConfig(
            failure_threshold=1,
            recovery_timeout=0,
            monitor_interval=3600,
            expected_exception=ValueError,
            ignored_exceptions=(AdmissionRejected,),
        ),
    )
    yield instance
    await instance.shutdown()


async def test_only_one_recovery_probe_reaches_provider(breaker):
    await breaker.force_open()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe():
        entered.set()
        await release.wait()
        return "recovered"

    task = asyncio.create_task(breaker.call(probe))
    second = AsyncMock(return_value="should not run")
    try:
        await entered.wait()
        with pytest.raises(CircuitBreakerOpenError):
            await breaker.call(second)
        second.assert_not_awaited()
        assert breaker.get_stats()["total_requests"] == 1
    finally:
        release.set()
        await task
    assert breaker.get_state() is CircuitState.CLOSED


@pytest.mark.parametrize("error", [asyncio.CancelledError, AdmissionRejected, RuntimeError])
async def test_inconclusive_probe_releases_slot_without_claiming_recovery(breaker, error):
    await breaker.force_open()
    initial_failures = breaker.get_stats()["failure_count"]
    with pytest.raises(error):
        await breaker.call(AsyncMock(side_effect=error()))

    assert breaker.get_state() is CircuitState.OPEN
    assert breaker.get_stats()["failure_count"] == initial_failures
    assert breaker.get_stats()["total_failures"] == 0
    assert await breaker.call(AsyncMock(return_value="recovered")) == "recovered"
    assert breaker.get_state() is CircuitState.CLOSED


async def test_old_success_cannot_close_breaker_during_recovery_probe(breaker):
    old_entered = asyncio.Event()
    old_release = asyncio.Event()
    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    async def old_request():
        old_entered.set()
        await old_release.wait()
        return "old success"

    async def probe():
        probe_entered.set()
        await probe_release.wait()
        raise ValueError("still unavailable")

    old_task = asyncio.create_task(breaker.call(old_request))
    probe_task = None
    try:
        await old_entered.wait()
        with pytest.raises(ValueError):
            await breaker.call(AsyncMock(side_effect=ValueError("outage")))
        probe_task = asyncio.create_task(breaker.call(probe))
        await probe_entered.wait()
        old_release.set()
        assert await old_task == "old success"
        assert breaker.get_stats()["total_successes"] == 1
        assert breaker.get_state() is CircuitState.HALF_OPEN
        with pytest.raises(CircuitBreakerOpenError):
            await breaker.call(AsyncMock())
        probe_release.set()
        with pytest.raises(ValueError):
            await probe_task
        assert breaker.get_state() is CircuitState.OPEN
        assert breaker.get_stats()["total_failures"] == 2
    finally:
        old_release.set()
        probe_release.set()
        await asyncio.gather(*([old_task, probe_task] if probe_task else [old_task]), return_exceptions=True)


@pytest.mark.parametrize("control", ["reset", "force_close", "force_open"])
async def test_manual_control_invalidates_inflight_failure(breaker, control):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending():
        entered.set()
        await release.wait()
        raise ValueError("obsolete failure")

    task = asyncio.create_task(breaker.call(pending))
    try:
        await entered.wait()
        await getattr(breaker, control)()
        expected_state = breaker.get_state()
        expected_failures = breaker.get_stats()["failure_count"]
        release.set()
        with pytest.raises(ValueError):
            await task
        assert breaker.get_state() is expected_state
        assert breaker.get_stats()["failure_count"] == expected_failures
        assert breaker.get_stats()["total_failures"] == (0 if control == "reset" else 1)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

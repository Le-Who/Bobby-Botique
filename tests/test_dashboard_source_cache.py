"""Concurrent dashboard readers share bounded, explicitly aged source results."""

import asyncio

import pytest

from app.dashboard_sources import DashboardSourceCache


@pytest.mark.asyncio
async def test_twenty_readers_share_one_refresh_and_return_copies():
    calls = 0
    gate = asyncio.Event()

    async def read():
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"count": 7}

    cache = DashboardSourceCache({"traffic": read})
    readers = [asyncio.create_task(cache.get("traffic", max_age_seconds=15)) for _ in range(20)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*readers)
    assert calls == 1
    assert all(row.available and not row.stale and row.value == {"count": 7} for row in results)
    results[0].value["count"] = 99
    assert (await cache.get("traffic", max_age_seconds=15)).value == {"count": 7}
    await cache.close()


@pytest.mark.asyncio
async def test_expiry_failure_retains_stale_then_becomes_unavailable():
    clock = [0.0]
    failed = False

    async def read():
        if failed:
            raise ConnectionError("synthetic; never expose this message")
        return {"count": 12}

    cache = DashboardSourceCache({"traffic": read}, clock=lambda: clock[0])
    first = await cache.get("traffic", max_age_seconds=15)
    failed = True
    clock[0] = 16
    stale = await cache.get("traffic", max_age_seconds=15)
    assert stale.value == {"count": 12}
    assert stale.available and stale.stale
    assert stale.observed_at == first.observed_at
    assert stale.reason_code == "source_failed"
    clock[0] = 61
    unavailable = await cache.get("traffic", max_age_seconds=15)
    assert not unavailable.available
    assert unavailable.value is None
    assert "synthetic" not in repr(unavailable)
    await cache.close()


@pytest.mark.asyncio
async def test_reader_cancellation_does_not_cancel_shared_refresh():
    gate = asyncio.Event()

    async def read():
        await gate.wait()
        return 3

    cache = DashboardSourceCache({"health": read})
    first = asyncio.create_task(cache.get("health", max_age_seconds=15))
    second = asyncio.create_task(cache.get("health", max_age_seconds=15))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert (await second).value == 3
    await cache.close()


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_invalidation_refreshes_only_one_source():
    calls = {"queue": 0, "traffic": 0}

    async def queue():
        calls["queue"] += 1
        return calls["queue"]

    async def traffic():
        calls["traffic"] += 1
        return 12

    async def hung():
        await asyncio.Event().wait()

    cache = DashboardSourceCache({"queue": queue, "traffic": traffic, "health": hung}, timeout_seconds=0.01)
    await cache.get("queue", max_age_seconds=5)
    await cache.get("traffic", max_age_seconds=15)
    cache.invalidate("queue")
    await cache.get("queue", max_age_seconds=5)
    await cache.get("traffic", max_age_seconds=15)
    assert calls == {"queue": 2, "traffic": 1}
    timed_out = await cache.get("health", max_age_seconds=15)
    assert not timed_out.available and timed_out.reason_code == "source_timeout"
    await cache.close()


@pytest.mark.asyncio
async def test_invalidation_during_read_reconciles_one_burst():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return {"version": calls}

    cache = DashboardSourceCache({"queue": read})
    reader = asyncio.create_task(cache.get("queue", max_age_seconds=5))
    await started.wait()
    for _ in range(20):
        cache.invalidate("queue")
    release.set()
    assert (await reader).value == {"version": 2}
    assert calls == 2
    await cache.close()


def test_http_alive_cannot_imply_bot_ready():
    from app.dashboard_sources import BotLifecycle

    lifecycle = BotLifecycle()
    for state in ("stopped", "starting", "draining", "failed"):
        lifecycle.set(state)
        assert lifecycle.snapshot()["ready"] is False
    lifecycle.set("starting")
    lifecycle.set("ready")
    assert lifecycle.snapshot()["ready"] is True
    lifecycle.set("failed")
    lifecycle.set("stopped")
    assert lifecycle.snapshot()["state"] == "failed"


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_owned_refresh():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def read():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    cache = DashboardSourceCache({"health": read})
    reader = asyncio.create_task(cache.get("health", max_age_seconds=15))
    await started.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert stopped.is_set()
    assert (await cache.get("health", max_age_seconds=15)).reason_code == "cache_closed"

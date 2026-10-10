"""Owned admission tasks finish before caller cancellation or lifecycle stop."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.webhook_admission import AdmissionClosed, WebhookAdmissionGate


async def _checkpoint():
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


async def _finish(*tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_started_admission_keeps_lock_until_child_finishes_after_repeated_cancellation():
    gate = WebhookAdmissionGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()
    finished = []

    async def first_operation():
        entered.set()
        await release.wait()
        finished.append("first")
        return 1

    async def second_operation():
        second_entered.set()
        finished.append("second")
        return 2

    first = asyncio.create_task(gate.admit(first_operation))
    second = None
    try:
        await entered.wait()
        first.cancel("first reason")
        await _checkpoint()
        second = asyncio.create_task(gate.admit(second_operation))
        first.cancel("second reason")
        await _checkpoint()
        assert not first.done()
        assert not second_entered.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await first
        assert cancelled.value.args == ("first reason",)
        assert await second == 2
        assert finished == ["first", "second"]
    finally:
        release.set()
        await _finish(first, *([second] if second is not None else []))


async def test_cancelled_waiter_never_starts_an_operation():
    gate = WebhookAdmissionGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    operations = []

    async def held():
        entered.set()
        await release.wait()

    async def waiting():
        operations.append("waiting")

    first = asyncio.create_task(gate.admit(held))
    second = None
    try:
        await entered.wait()
        second = asyncio.create_task(gate.admit(waiting))
        await _checkpoint()
        second.cancel("unclaimed request disconnected")
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        await first
        assert operations == []
        await gate.admit(waiting)
        assert operations == ["waiting"]
    finally:
        release.set()
        await _finish(first, *([second] if second is not None else []))


async def test_dependency_self_cancellation_propagates_and_releases_lock():
    gate = WebhookAdmissionGate()

    async def cancelled_dependency():
        raise asyncio.CancelledError("dependency cancelled itself")

    async def fresh():
        return "next admitted"

    with pytest.raises(asyncio.CancelledError) as cancelled:
        await asyncio.wait_for(gate.admit(cancelled_dependency), timeout=1)
    assert cancelled.value.args == ("dependency cancelled itself",)
    assert await gate.admit(fresh) == "next admitted"


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_operation_failure_is_observed_and_lock_is_released(caplog, cancel_owner):
    gate = WebhookAdmissionGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("synthetic operation failure")

    async def failing():
        entered.set()
        await release.wait()
        raise failure

    async def fresh():
        return 7

    owner = asyncio.create_task(gate.admit(failing))
    try:
        await entered.wait()
        if cancel_owner:
            owner.cancel("original disconnect")
            await _checkpoint()
            owner.cancel("later disconnect")
        with caplog.at_level(logging.ERROR):
            release.set()
            with pytest.raises(asyncio.CancelledError if cancel_owner else RuntimeError) as raised:
                await owner
        if cancel_owner:
            assert raised.value.args == ("original disconnect",)
            assert raised.value.__cause__ is failure
            assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)
        else:
            assert raised.value is failure
        assert await gate.admit(fresh) == 7
    finally:
        release.set()
        await _finish(owner)


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_owner_cancellation_wins_when_child_finishes_in_the_same_loop_turn(cancel_first):
    gate = WebhookAdmissionGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    delivered = []

    async def operation():
        entered.set()
        await release.wait()
        delivered.append(42)

    owner = asyncio.create_task(gate.admit(operation))
    try:
        await entered.wait()
        if cancel_first:
            owner.cancel("simultaneous disconnect")
            release.set()
        else:
            release.set()
            owner.cancel("simultaneous disconnect")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("simultaneous disconnect",)
        assert delivered == [42]
    finally:
        release.set()
        await _finish(owner)


async def test_shutdown_closes_waiters_then_drains_and_stops_despite_repeated_cancellation():
    gate = WebhookAdmissionGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    order = []

    async def operation():
        entered.set()
        await release.wait()
        order.append("handoff")

    async def unclaimed():
        order.append("unexpected claim")

    async def stop():
        order.append("stop")
        stop_entered.set()
        await release_stop.wait()

    admission = asyncio.create_task(gate.admit(operation))
    waiter = shutdown = repeated = None
    try:
        await entered.wait()
        waiter = asyncio.create_task(gate.admit(unclaimed))
        await _checkpoint()
        shutdown = asyncio.create_task(gate.shutdown(stop))
        await _checkpoint()
        with pytest.raises(AdmissionClosed):
            await gate.admit(unclaimed)
        shutdown.cancel("first shutdown cancel")
        await _checkpoint()
        shutdown.cancel("second shutdown cancel")
        await _checkpoint()
        assert not shutdown.done()
        assert not stop_entered.is_set()
        release.set()
        await admission
        with pytest.raises(AdmissionClosed):
            await waiter
        await stop_entered.wait()
        repeated = asyncio.create_task(gate.shutdown(stop))
        shutdown.cancel("third shutdown cancel")
        await _checkpoint()
        assert not shutdown.done()
        assert order == ["handoff", "stop"]
        release_stop.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await shutdown
        assert cancelled.value.args == ("first shutdown cancel",)
        await repeated
        await gate.shutdown(stop)
        assert order == ["handoff", "stop"]
    finally:
        release.set()
        release_stop.set()
        await _finish(admission, *(task for task in (waiter, shutdown, repeated) if task is not None))


async def test_failed_stop_remains_one_observed_terminal_shutdown():
    gate = WebhookAdmissionGate()
    stops = []
    failure = RuntimeError("synthetic stop failure")

    async def stop():
        stops.append("stop")
        raise failure

    async def unexpected_admission():
        pytest.fail("Closed admission started an operation")

    for _ in range(2):
        with pytest.raises(RuntimeError) as raised:
            await gate.shutdown(stop)
        assert raised.value is failure
    assert stops == ["stop"]
    with pytest.raises(AdmissionClosed):
        await gate.admit(unexpected_admission)


@pytest.mark.parametrize("stop_failed", [False, True], ids=["completed-stop", "failed-stop"])
async def test_guarded_error_cleanup_still_cleans_other_resources_after_terminal_stop(monkeypatch, stop_failed):
    """Wrapping all cleanup in the shared stop task would skip these resources."""
    import bot

    gate = WebhookAdmissionGate()
    stops = []
    resources = []

    async def stop():
        stops.append("stop")
        if stop_failed:
            raise RuntimeError("synthetic terminal stop failure")

    if stop_failed:
        with pytest.raises(RuntimeError, match="synthetic terminal stop failure"):
            await gate.shutdown(stop)
    else:
        await gate.shutdown(stop)

    def resource(name):
        async def cleanup(*args, **kwargs):
            resources.append(name)
            return 0

        return cleanup

    for target, name in (
        ("app.admin_alerts.alert_admin_shutdown", "alert"),
        ("app.repos.memory_autosave.drain_pending_memory_writes", "memory"),
        ("app.repos.memory_autosave.pre_shutdown_compact", "compact"),
        ("bot.stop_task_queue", "queue"),
        ("app.providers.close_http_clients", "http"),
        ("app.search_services.close_tavily_client", "tavily"),
        ("app.intent_router.close_http_client", "intent"),
        ("app.cache.shutdown_redis", "redis"),
        ("app.memory_manager.shutdown_memory_manager", "memory_manager"),
    ):
        monkeypatch.setattr(target, resource(name))
    monkeypatch.setattr("app.state._pending_persists", {})
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: resources.append("images"))
    application = SimpleNamespace(updater=None, _initialized=True, stop=stop)
    await bot._cleanup_application(application, webhook_admission=gate)
    assert stops == ["stop"]
    assert resources == [
        "alert",
        "memory",
        "compact",
        "queue",
        "http",
        "tavily",
        "intent",
        "redis",
        "images",
        "memory_manager",
    ]

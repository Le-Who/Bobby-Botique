"""Ingress capacity must survive PTB fetching work before user-lane admission."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import Application
from telegram.request import BaseRequest

from app.update_processor import UserScopedUpdateProcessor
from app.update_queue import OutstandingUpdateQueue


class _OfflineRequest(BaseRequest):
    read_timeout = 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        assert url.endswith("/getMe") and method == "POST"
        return 200, json.dumps({"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Test"}}).encode()


@dataclass
class _Update:
    update_id: int
    handler: Callable[[], Awaitable[None]]
    user_id: int = 7
    inline_query: object | None = None

    @property
    def effective_user(self):
        return SimpleNamespace(id=self.user_id)


class _HandledApplication(Application):
    async def process_update(self, update):
        await update.handler()


@pytest.fixture
def application(monkeypatch):
    from app.runtime_settings.store import SettingsSnapshot

    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(
        "app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(0, MappingProxyType({})))
    )
    return (
        Application.builder()
        .token("123:synthetic-token")
        .request(_OfflineRequest())
        .get_updates_request(_OfflineRequest())
        .updater(None)
        .job_queue(None)
        .application_class(_HandledApplication)
        .update_queue(OutstandingUpdateQueue(100))
        .concurrent_updates(UserScopedUpdateProcessor(50))
        .build()
    )


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


async def test_real_ptb_fetcher_cannot_turn_polling_backlog_into_unbounded_wrappers(application, caplog):
    """Ten thousand updates cannot bypass the bound after PTB fetches them."""
    # This verifies capacity/drain, not the file writer's throughput. A previous
    # logging integration test may leave debug handlers configured in the suite.
    # Keep warnings/errors and all actual update/queue/processor behavior active.
    caplog.set_level(logging.WARNING, logger="app.observability")
    caplog.set_level(logging.WARNING, logger="telegram.ext.Application")
    queue = application.update_queue
    release = asyncio.Event()
    handled = []

    def handler(update_id):
        async def run():
            await release.wait()
            handled.append(update_id)

        return run

    producers = []
    async with application:
        await application.start()
        try:
            for update_id in range(10_000):
                producers.append(asyncio.create_task(queue.put(_Update(update_id, handler(update_id)))))
            await _until(lambda: queue.empty() and application.update_processor._user_locks.get(7) is not None)
            # Let every runnable producer/fetcher/wrapper reach its blocking point.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert queue.full(), "Fetching must not release ingress capacity before task_done"
            assert queue.outstanding == 100
            assert queue.buffered == queue.qsize() == 0
            assert sum(task.done() for task in producers) == 100
            assert application.update_processor._user_locks[7].references == 100
            processing = [
                task for task in asyncio.all_tasks() if task.get_name().endswith(":process_concurrent_update")
            ]
            assert len(processing) == 100
            with pytest.raises(asyncio.QueueFull):
                queue.put_nowait(_Update(10_001, handler(10_001)))
        finally:
            release.set()
            await asyncio.gather(*producers)
            await asyncio.wait_for(queue.join(), 5)
            await application.stop()
    assert handled == list(range(10_000))
    assert queue.outstanding == queue.buffered == 0


async def test_get_keeps_capacity_until_terminal_task_done_and_polling_put_waits():
    queue = OutstandingUpdateQueue(1)
    queue.put_nowait("first")
    assert await queue.get() == "first"
    waiting = asyncio.create_task(queue.put("second"))
    try:
        await asyncio.sleep(0)
        assert not waiting.done(), "A fetched unfinished update must keep the polling producer blocked"
        assert queue.full()
        queue.task_done()
        await asyncio.wait_for(waiting, 1)
        assert queue.get_nowait() == "second"
        assert queue.outstanding == 1
        queue.task_done()
        await asyncio.wait_for(queue.join(), 1)
        with pytest.raises(ValueError, match="task_done"):
            queue.task_done()
        assert queue.outstanding == 0
    finally:
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("capacity", [0, -1])
def test_nonpositive_capacity_cannot_disable_the_ingress_bound(capacity):
    with pytest.raises(ValueError, match="positive"):
        OutstandingUpdateQueue(capacity)


@pytest.mark.parametrize("cancel_after_wakeup", [False, True])
async def test_cancelled_polling_producer_does_not_steal_the_next_completion(cancel_after_wakeup):
    queue = OutstandingUpdateQueue(1)
    queue.put_nowait("first")
    assert queue.get_nowait() == "first"
    cancelled = asyncio.create_task(queue.put("cancelled"))
    retained = asyncio.create_task(queue.put("retained"))
    try:
        await asyncio.sleep(0)
        if cancel_after_wakeup:
            queue.task_done()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        if not cancel_after_wakeup:
            queue.task_done()
        await asyncio.wait_for(retained, 1)
        assert queue.get_nowait() == "retained"
        queue.task_done()
        await asyncio.wait_for(queue.join(), 1)
        assert queue.outstanding == 0
    finally:
        for task in (cancelled, retained):
            if not task.done():
                task.cancel()
        await asyncio.gather(cancelled, retained, return_exceptions=True)


async def test_real_ptb_already_admitted_other_user_and_inline_bypass_hot_user_backlog(application):
    queue = application.update_queue
    release = asyncio.Event()
    other_started = asyncio.Event()
    inline_started = asyncio.Event()

    async def blocked():
        await release.wait()

    async def other():
        other_started.set()

    async def inline():
        inline_started.set()

    for update_id in range(98):
        queue.put_nowait(_Update(update_id, blocked))
    queue.put_nowait(_Update(98, other, user_id=8))
    queue.put_nowait(_Update(99, inline, inline_query=object()))
    assert queue.full()
    async with application:
        await application.start()
        try:
            await asyncio.wait_for(other_started.wait(), 1)
            await asyncio.wait_for(inline_started.wait(), 1)
            assert not release.is_set()
            assert queue.outstanding == 98
        finally:
            release.set()
            await application.stop()
    assert queue.outstanding == 0


async def test_real_ptb_wrapper_releases_capacity_after_raised_handler(application):
    queue = application.update_queue
    errors = []

    async def failed():
        raise RuntimeError("synthetic processing failure")

    async def observe_error(update, context):
        errors.append(context.error)

    application.add_error_handler(observe_error)
    async with application:
        await application.start()
        try:
            await queue.put(_Update(1, failed))
            await asyncio.wait_for(queue.join(), 1)
            await _until(lambda: bool(errors))
            assert isinstance(errors[0], RuntimeError)
            assert str(errors[0]) == "synthetic processing failure"
            assert queue.outstanding == queue.buffered == 0
        finally:
            await application.stop()


async def test_real_ptb_wrapper_cancelled_before_processing_slot_releases_capacity(application):
    queue = application.update_queue
    release = asyncio.Event()
    entered = asyncio.Event()
    unstarted = []

    async def first():
        entered.set()
        await release.wait()

    async def queued():
        unstarted.append("unexpected handler execution")

    async with application:
        await application.start()
        try:
            await queue.put(_Update(1, first))
            await entered.wait()
            existing = set(asyncio.all_tasks())
            await queue.put(_Update(2, queued))
            await _until(lambda: application.update_processor._user_locks[7].references == 2)
            waiter = next(
                task
                for task in asyncio.all_tasks() - existing
                if task.get_name().endswith(":process_concurrent_update")
            )
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert unstarted == []
            assert queue.outstanding == 1
            release.set()
            await asyncio.wait_for(queue.join(), 1)
            assert queue.outstanding == 0
        finally:
            release.set()
            await application.stop()


@pytest.mark.parametrize("fetched_before_stop", [False, True], ids=["buffered-drain", "fetched-drain"])
async def test_real_ptb_stop_sentinel_waits_for_capacity_then_drains(application, fetched_before_stop):
    queue = application.update_queue
    release = asyncio.Event()
    completed = []

    async def handler():
        await release.wait()
        completed.append(True)

    for update_id in range(100):
        queue.put_nowait(_Update(update_id, handler))
    async with application:
        await application.start()
        if fetched_before_stop:
            await _until(lambda: queue.empty())
        stopping = asyncio.create_task(application.stop())
        try:
            await asyncio.sleep(0)
            assert not stopping.done()
            assert queue.full()
            assert queue.outstanding == 100
            release.set()
            await asyncio.wait_for(stopping, 2)
            await asyncio.wait_for(queue.join(), 1)
            assert len(completed) == 100
            assert queue.outstanding == queue.buffered == 0
        finally:
            release.set()
            await stopping

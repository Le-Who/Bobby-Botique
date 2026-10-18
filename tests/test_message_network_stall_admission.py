"""Network-stall retries retain admission until the old request retires.

The handler, canonical state, debounce, heartbeat retirement and TaskManager stay
real. Events replace elapsed-time races at the existing 150 ms recheck and the
notice timer; synthetic Telegram and downstream provider work do no external I/O.
"""

import asyncio
import time
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import security, state
from app.database import db_manager
from app.handlers import messages
from app.i18n import t
from app.metrics import MetricsCollector
from app.middleware import debounce, dedup
from app.utils import heartbeat
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


async def checkpoint_until(predicate, owner):
    """Advance runnable work to an observable boundary, without clock sleeps."""
    for _ in range(100):
        if predicate():
            return
        if owner.done():
            await owner
            raise AssertionError("owner finished before the expected boundary")
        ready = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(ready.set_result, None)
        await ready
    raise AssertionError("owner did not reach the expected scheduler boundary")


class Work:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()
        self.finished = asyncio.Event()


class Placeholder:
    def __init__(self, index, chat):
        self.message_id = 1000 + index
        self.chat_id = chat.id
        self.chat = chat
        self.writes = []
        self.notice_entered = asyncio.Event()
        self.notice_cleanup_entered = asyncio.Event()
        self.release_notice_cleanup = asyncio.Event()
        self.release_notice_cleanup.set()
        self.notice_finished = asyncio.Event()
        self.gate_notice = False

    async def edit_text(self, text, **kwargs):
        markup = kwargs.get("reply_markup")
        buttons = [button.callback_data for row in markup.inline_keyboard for button in row] if markup else []
        if f"retry_wait:{self.message_id}" in buttons and self.gate_notice:
            self.notice_entered.set()
            try:
                await asyncio.Future()
            finally:
                self.notice_cleanup_entered.set()
                await self.release_notice_cleanup.wait()
                self.notice_finished.set()
        self.writes.append(text)
        return self

    async def delete(self):
        pass


@pytest.fixture
async def network_flow(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=8))
    for name in ("_ACTIVE_TASKS", "_LAST_BOT_MESSAGE", "_NETWORK_STALL_SINCE", "_pending_persists"):
        monkeypatch.setattr(state, name, {})
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repos.users.save_user_state", AsyncMock())
    # User 1 is the synthetic administrator; user 2 takes the real auth-cache path.
    monkeypatch.setattr(db_manager, "_user_auth_cache", {2: True})
    monkeypatch.setattr(security, "rate_limiter", security.RateLimiter())
    monkeypatch.setattr(messages, "metrics_collector", MetricsCollector())
    monkeypatch.setattr(dedup, "_recent_requests", defaultdict(dict))
    monkeypatch.setattr(debounce, "_debounce_slots", {})
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 60)
    for name in ("_HEARTBEAT_EVENTS", "_HEARTBEAT_TASKS", "_HEARTBEAT_OWNERS", "_WAIT_NOTICE_TASKS"):
        monkeypatch.setattr(heartbeat, name, {})

    clock = SimpleNamespace(now=100.0)
    state_time = SimpleNamespace(**vars(time))
    state_time.monotonic = lambda: clock.now
    monkeypatch.setattr(state, "time", state_time)
    rechecks = {}
    notice_threshold = asyncio.Event()

    async def controlled_sleep(delay):
        if delay != 0.15:
            return await asyncio.sleep(delay)
        owner = asyncio.current_task()
        gate = asyncio.Event()
        rechecks[owner] = gate
        await gate.wait()

    async def controlled_wait(awaitable, *, timeout):
        if timeout != 60.0:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        stopped = asyncio.create_task(awaitable)
        elapsed = asyncio.create_task(notice_threshold.wait())
        try:
            done, _ = await asyncio.wait({stopped, elapsed}, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                return stopped.result()
            raise TimeoutError
        finally:
            for task in (stopped, elapsed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stopped, elapsed, return_exceptions=True)

    router_asyncio = SimpleNamespace(**vars(asyncio))
    router_asyncio.sleep = controlled_sleep
    router_asyncio.wait_for = controlled_wait
    monkeypatch.setattr(messages, "asyncio", router_asyncio)
    manager = TaskManager()
    owners, submitted, heartbeat_tasks = [], {}, []

    def submit(coroutine, **kwargs):
        task = manager.submit(coroutine, **kwargs)
        submitted[task] = coroutine
        return task

    monkeypatch.setattr(messages, "submit_task", submit)
    register = messages.register_heartbeat

    def register_heartbeat(mid, event, chat):
        register(mid, event, chat)
        heartbeat_tasks.append(heartbeat._HEARTBEAT_TASKS[mid])

    monkeypatch.setattr(messages, "register_heartbeat", register_heartbeat)
    work, placeholders, replies, contexts = {}, {}, defaultdict(list), {}
    entered = []

    async def process(_placeholder, update, _context, **_kwargs):
        index, uid = update.update_id, update.effective_user.id
        item = work[index]
        assert state.get_user_lock(uid).locked()
        entered.append((uid, index))
        item.entered.set()
        try:
            await item.release.wait()
        finally:
            item.cleanup_entered.set()
            await item.release_cleanup.wait()
            item.finished.set()

    monkeypatch.setattr("app.handlers.agent.process_long_request", process)

    async def launch(index, *, uid=1, kind="text"):
        work[index] = Work()
        chat = SimpleNamespace(id=uid, type="private", send_action=AsyncMock())
        placeholders[index] = Placeholder(index, chat)

        async def reply(text, **_kwargs):
            replies[index].append(text)
            return placeholders[index]

        msg = SimpleNamespace(
            message_id=100 + index,
            chat_id=uid,
            chat=chat,
            text=f"Discuss synthetic attempt {index}" if kind == "text" else None,
            caption=None,
            photo=(SimpleNamespace(file_id="synthetic-photo"),) if kind == "photo" else (),
            voice=None,
            document=None,
            media_group_id=None,
            forward_origin=None,
            reply_to_message=None,
            reply_text=reply,
        )
        update = SimpleNamespace(
            update_id=index,
            message=msg,
            effective_message=msg,
            effective_user=SimpleNamespace(id=uid),
            effective_chat=chat,
        )
        context = contexts.setdefault(
            uid,
            SimpleNamespace(
                user_data={},
                bot=SimpleNamespace(username="synthetic_bot", id=999, set_message_reaction=AsyncMock()),
            ),
        )
        owner = asyncio.create_task(messages.handle_request(update, context))
        owners.append(owner)
        if kind == "text":
            await checkpoint_until(lambda: uid in debounce._debounce_slots, owner)
            debounce._debounce_slots[uid].ready_event.set()
        return owner

    async def start(index=1, **kwargs):
        await (await launch(index, **kwargs))
        await work[index].entered.wait()
        return state.get_active_task(kwargs.get("uid", 1))

    async def reach_recheck(owner):
        await checkpoint_until(lambda: owner in rechecks, owner)

    def mark_stalled(uid=1):
        state.mark_network_waiting(uid)
        clock.now += 15.001

    try:
        yield SimpleNamespace(
            launch=launch,
            start=start,
            reach_recheck=reach_recheck,
            rechecks=rechecks,
            mark_stalled=mark_stalled,
            clock=clock,
            work=work,
            placeholders=placeholders,
            replies=replies,
            entered=entered,
            notice_threshold=notice_threshold,
        )
    finally:
        for item in work.values():
            item.release.set()
            item.release_cleanup.set()
        for placeholder in placeholders.values():
            placeholder.release_notice_cleanup.set()
        for gate in rechecks.values():
            gate.set()
        for owner in owners:
            if not owner.done():
                owner.cancel()
        await asyncio.gather(*owners, return_exceptions=True)
        await manager.drain(timeout=0)
        for task in heartbeat_tasks:
            task.cancel()
        await asyncio.gather(*heartbeat_tasks, return_exceptions=True)
        for coroutine in submitted.values():
            coroutine.close()
        for handle in state._pending_persists.values():
            handle.cancel()


@pytest.mark.parametrize("kind", ["text", "photo"])
async def test_stall_retry_keeps_busy_reservation_while_notice_retires(network_flow, kind):
    """Checking only the unlocked user lock admits work before notice cleanup."""
    flow = network_flow
    old = await flow.start()
    placeholder = flow.placeholders[1]
    placeholder.gate_notice = True
    placeholder.release_notice_cleanup.clear()
    flow.notice_threshold.set()
    await placeholder.notice_entered.wait()
    flow.mark_stalled()

    retry = await flow.launch(2, kind=kind)
    await flow.reach_recheck(retry)
    await placeholder.notice_cleanup_entered.wait()
    assert not state.get_user_lock(1).locked()
    assert state.get_active_task(1) is old
    assert state.get_user_state(1).is_processing
    assert not old.done()
    flow.rechecks[retry].set()
    await retry

    assert flow.replies[2] == [t("busy.toast", "en" if kind == "text" else "ru")]
    assert flow.entered == [(1, 1)]
    assert state.get_active_task(1) is old
    assert state.get_user_state(1).is_processing
    placeholder.release_notice_cleanup.set()
    await old
    assert placeholder.notice_finished.is_set()
    assert state.get_active_task(1) is None
    assert not state.get_user_state(1).is_processing
    await flow.start(3, kind=kind)
    assert flow.entered == [(1, 1), (1, 3)]


@pytest.mark.parametrize("original_kind", ["text", "photo"])
@pytest.mark.parametrize("retry_kind", ["text", "photo"])
async def test_quick_stall_cleanup_allows_replacement_after_retirement(network_flow, original_kind, retry_kind):
    """A cancelled owner must release its resources before replacement starts."""
    flow = network_flow
    old = await flow.start(kind=original_kind)
    flow.work[1].release_cleanup.clear()
    flow.mark_stalled()
    retry = await flow.launch(2, kind=retry_kind)
    await flow.reach_recheck(retry)
    await flow.work[1].cleanup_entered.wait()
    assert state.get_active_task(1) is old
    assert state.get_user_state(1).is_processing
    assert state.get_user_lock(1).locked()
    assert not flow.replies[2]
    assert flow.entered == [(1, 1)]

    flow.work[1].release_cleanup.set()
    await old
    assert flow.work[1].finished.is_set()
    assert state.get_active_task(1) is None
    assert not state.get_user_state(1).is_processing
    assert not state.get_user_lock(1).locked()
    assert not heartbeat._HEARTBEAT_EVENTS
    assert not heartbeat._WAIT_NOTICE_TASKS
    flow.rechecks[retry].set()
    await retry
    await flow.work[2].entered.wait()

    replacement = state.get_active_task(1)
    assert replacement is not None and replacement is not old
    assert flow.entered == [(1, 1), (1, 2)]
    assert len(flow.replies[2]) == 1
    assert state.get_user_state(1).is_processing
    assert state.get_user_lock(1).locked()
    assert heartbeat.is_heartbeat_active(flow.placeholders[2].message_id, chat_id=1)


@pytest.mark.parametrize("kind", ["text", "photo"])
async def test_delayed_stall_cleanup_rejects_retries_and_other_user_progresses(network_flow, kind):
    """A retry cannot recancel cooperative cleanup or displace its owner."""
    flow = network_flow
    old = await flow.start(kind=kind)
    flow.work[1].release_cleanup.clear()
    flow.mark_stalled()

    for index in (2, 3):
        retry = await flow.launch(index, kind=kind)
        await flow.reach_recheck(retry)
        await flow.work[1].cleanup_entered.wait()
        flow.rechecks[retry].set()
        await retry
        assert flow.replies[index] == [t("busy.toast", "en" if kind == "text" else "ru")]
        assert state.get_active_task(1) is old
        assert state.get_user_state(1).is_processing
        assert state.get_user_lock(1).locked()
        assert old.cancelling() == 1
        assert not flow.work[1].finished.is_set()
        assert flow.entered == [(1, 1)]

    other = await flow.start(4, uid=2)
    assert flow.entered == [(1, 1), (2, 4)]
    assert state.get_active_task(1) is old
    assert state.get_active_task(2) is other
    assert other is not None and not other.cancelling()
    assert state.get_user_lock(2).locked()
    flow.work[1].release_cleanup.set()
    await old
    assert flow.work[1].finished.is_set()
    assert state.get_active_task(1) is None
    assert not state.get_user_state(1).is_processing
    assert state.get_active_task(2) is other
    assert state.get_user_state(2).is_processing
    assert heartbeat.is_heartbeat_active(flow.placeholders[4].message_id, chat_id=2)

    await flow.start(5, kind=kind)
    assert flow.entered == [(1, 1), (2, 4), (1, 5)]


@pytest.mark.parametrize("status", ["unmarked", "under_threshold", "at_threshold", "receiving"])
async def test_healthy_request_keeps_owner_and_busy_rejects_new_message(network_flow, status):
    """Missing/short network waits and resumed streams must not be cancelled."""
    flow = network_flow
    old = await flow.start()
    if status != "unmarked":
        state.mark_network_waiting(1)
        flow.clock.now += {"under_threshold": 14.999, "at_threshold": 15.0, "receiving": 16.0}[status]
        if status == "receiving":
            state.mark_network_alive(1)
    retry = await flow.launch(2)
    await retry

    assert flow.replies[2] == [t("busy.toast", "en")]
    assert flow.entered == [(1, 1)]
    assert state.get_active_task(1) is old
    assert not old.cancelling()
    assert not flow.work[1].cleanup_entered.is_set()
    assert state.get_user_state(1).is_processing
    assert state.get_user_lock(1).locked()
    assert not flow.rechecks


@pytest.mark.parametrize("kind", ["text", "photo"])
async def test_cancelled_recheck_leaves_old_cleanup_owned_until_later_retry(network_flow, kind):
    """Cancelling the retry itself must not release or restart the old request."""
    flow = network_flow
    old = await flow.start()
    flow.work[1].release_cleanup.clear()
    flow.mark_stalled()
    retry = await flow.launch(2, kind=kind)
    await flow.reach_recheck(retry)
    await flow.work[1].cleanup_entered.wait()
    retry.cancel("synthetic retry cancellation")
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await retry
    assert cancelled.value.args == ("synthetic retry cancellation",)
    assert not flow.replies[2]
    assert flow.entered == [(1, 1)]
    assert state.get_active_task(1) is old
    assert state.get_user_state(1).is_processing
    assert state.get_user_lock(1).locked()
    assert old.cancelling() == 1

    flow.work[1].release_cleanup.set()
    await old
    assert flow.work[1].finished.is_set()
    assert state.get_active_task(1) is None
    assert not state.get_user_state(1).is_processing
    await flow.start(3, kind=kind)
    assert flow.entered == [(1, 1), (1, 3)]


@pytest.mark.parametrize("kind", ["text", "photo"])
async def test_second_stall_recheck_cannot_displace_a_new_admission_owner(network_flow, monkeypatch, kind):
    """Two retries that see the same stalled owner must admit at most one."""
    flow = network_flow
    old = await flow.start()
    flow.work[1].release_cleanup.clear()
    flow.mark_stalled()
    first = await flow.launch(2)
    await flow.reach_recheck(first)
    await flow.work[1].cleanup_entered.wait()
    second = await flow.launch(3, kind=kind)
    await flow.reach_recheck(second)
    assert old.cancelling() == 1
    flow.work[1].release_cleanup.set()
    await old
    assert not state.get_user_state(1).is_processing

    intent_entered, release_intent = asyncio.Event(), asyncio.Event()

    async def intent(_text):
        intent_entered.set()
        await release_intent.wait()

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    try:
        flow.rechecks[first].set()
        await intent_entered.wait()
        assert state.get_active_task(1) is first
        assert state.get_user_state(1).is_processing
        assert not state.get_user_lock(1).locked()
        flow.rechecks[second].set()
        await second

        assert flow.replies[3] == [t("busy.toast", "en" if kind == "text" else "ru")]
        assert not flow.replies[2]
        assert flow.entered == [(1, 1)]
        assert state.get_active_task(1) is first
        assert state.get_user_state(1).is_processing
        release_intent.set()
        await first
        await flow.work[2].entered.wait()
        assert flow.entered == [(1, 1), (1, 2)]
    finally:
        release_intent.set()

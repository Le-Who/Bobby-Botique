"""Failed placeholder I/O must release admission before a worker takes over.

Removing the pre-worker reservation cleanup makes every failure case leave the
user permanently busy. Routing, state locks, debounce and heartbeat owners stay
real; synthetic Telegram/repository and downstream workload boundaries do no I/O.
"""

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from app import security, state
from app.handlers import messages
from app.metrics import MetricsCollector
from app.middleware import debounce, dedup
from app.utils import heartbeat
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


async def checkpoint_until(predicate, owner):
    """Let runnable tasks advance, without using a wall-clock race as an oracle."""
    for _ in range(100):
        if predicate():
            return
        if owner.done():
            await owner
            raise AssertionError("handler returned before reaching the expected boundary")
        checkpoint = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
        await checkpoint
    raise AssertionError("handler did not reach the expected scheduler boundary")


@pytest.fixture
async def admission(monkeypatch):
    uid = 1  # Synthetic configured administrator: real authorization has no SQL I/O.
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=8))
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_LAST_BOT_MESSAGE", {})
    monkeypatch.setattr(state, "_NETWORK_STALL_SINCE", {})
    monkeypatch.setattr(state, "_pending_persists", {})
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repos.users.save_user_state", AsyncMock())
    monkeypatch.setattr(security, "rate_limiter", security.RateLimiter())
    monkeypatch.setattr(messages, "metrics_collector", MetricsCollector())
    monkeypatch.setattr(dedup, "_recent_requests", defaultdict(dict))
    monkeypatch.setattr(dedup, "_recent_voice_ids", defaultdict(dict))
    monkeypatch.setattr(debounce, "_debounce_slots", {})
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 60)
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_EVENTS", {})
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_TASKS", {})
    manager = TaskManager()
    monkeypatch.setattr(messages, "submit_task", manager.submit)
    monkeypatch.setattr(debounce, "submit_task", manager.submit)
    owners = []
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def workload(kind, update):
        assert state.get_user_lock(uid).locked()
        calls.append((kind, update.update_id))
        started.set()
        await release.wait()

    async def voice(_placeholder, update, _context):
        await workload("voice", update)

    async def chat(_placeholder, update, _context, **_kwargs):
        await workload("chat", update)

    async def draw(update, _context, **_kwargs):
        await workload("draw", update)

    monkeypatch.setattr(messages, "handle_voice_inline", voice)
    monkeypatch.setattr("app.handlers.agent.process_long_request", chat)
    monkeypatch.setattr("app.handlers.cmd_image._run_generation", draw)

    def update(kind, index, reply):
        chat = SimpleNamespace(id=uid, type="private", send_action=AsyncMock())
        text = f"Discuss synthetic admission attempt {index}"
        if kind == "draw":
            text = f"Нарисуй синтетического кота {index}"
        msg = SimpleNamespace(
            message_id=100 + index,
            chat_id=uid,
            chat=chat,
            text=None if kind in {"voice", "photo"} else text,
            caption=None,
            voice=SimpleNamespace(file_unique_id=f"synthetic-voice-{index}", duration=2) if kind == "voice" else None,
            photo=(SimpleNamespace(file_id="synthetic-photo"),) if kind == "photo" else (),
            document=None,
            media_group_id=None,
            forward_origin=None,
            reply_to_message=None,
            reply_text=reply,
        )
        user = SimpleNamespace(id=uid, username="synthetic", first_name="Synthetic", language_code="en")
        return SimpleNamespace(
            update_id=index, message=msg, effective_message=msg, effective_user=user, effective_chat=chat
        )

    context = SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(username="synthetic_bot", id=999, set_message_reaction=AsyncMock()),
    )

    async def launch(kind, index, reply):
        item = update(kind, index, reply)
        owner = asyncio.create_task(messages.handle_request(item, context))
        owners.append(owner)
        if kind in {"text", "draw"}:
            await checkpoint_until(lambda: uid in debounce._debounce_slots, owner)
            debounce._debounce_slots[uid].ready_event.set()
        return owner

    try:
        yield SimpleNamespace(uid=uid, launch=launch, calls=calls, started=started, release=release)
    finally:
        release.set()
        for owner in owners:
            if not owner.done():
                owner.cancel()
        await asyncio.gather(*owners, return_exceptions=True)
        await manager.drain(timeout=0)
        heartbeats = list(heartbeat._HEARTBEAT_TASKS.values())
        for task in heartbeats:
            task.cancel()
        await asyncio.gather(*heartbeats, return_exceptions=True)
        for handle in state._pending_persists.values():
            handle.cancel()


@pytest.mark.parametrize("kind", ["voice", "text", "photo", "draw"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_placeholder_releases_reservation_and_next_request_runs(admission, kind, failure):
    placeholder_started = asyncio.Event()
    placeholder_finished = asyncio.Event()
    failure_error = NetworkError("Synthetic placeholder delivery failed")

    async def failed_reply(*_args, **_kwargs):
        assert state.get_user_state(admission.uid).is_processing
        placeholder_started.set()
        try:
            if failure == "error":
                raise failure_error
            await asyncio.Future()
        finally:
            placeholder_finished.set()

    owner = await admission.launch(kind, 1, failed_reply)
    await asyncio.wait_for(placeholder_started.wait(), 1)
    if failure == "cancel":
        owner.cancel("synthetic admission shutdown")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("synthetic admission shutdown",)
    else:
        with pytest.raises(NetworkError) as failed:
            await owner
        assert failed.value is failure_error

    assert placeholder_finished.is_set()
    assert admission.calls == []
    assert not state.get_user_lock(admission.uid).locked()
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS
    assert not debounce.has_open_slot(admission.uid)
    assert not state.get_user_state(admission.uid).is_processing, "failed admission must release its busy reservation"

    placeholder = SimpleNamespace(message_id=202, edit_text=AsyncMock(), delete=AsyncMock())
    retry = await admission.launch(kind, 2, AsyncMock(return_value=placeholder))
    await asyncio.wait_for(retry, 1)
    await asyncio.wait_for(admission.started.wait(), 1)
    expected = "voice" if kind == "voice" else "draw" if kind == "draw" else "chat"
    assert admission.calls == [(expected, 2)]
    assert state.get_user_state(admission.uid).is_processing
    assert state.get_user_lock(admission.uid).locked()
    admission.release.set()
    await checkpoint_until(lambda: not state.get_user_state(admission.uid).is_processing, asyncio.current_task())
    assert not state.get_user_lock(admission.uid).locked()
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS

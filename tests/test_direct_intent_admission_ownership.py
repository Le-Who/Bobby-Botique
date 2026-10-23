"""Intent I/O stays owned until asynchronous cancellation cleanup completes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import agent, messages
from app.utils import heartbeat
from tests import test_edited_request_ownership as ownership_support

pytestmark = pytest.mark.asyncio
ownership = ownership_support.ownership
settle = ownership_support.settle


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_cancelled_intent_releases_admission_after_owned_cleanup(monkeypatch, ownership, repeat_cancel):
    started, cleanup_started, release_cleanup, cleanup_complete = (asyncio.Event() for _ in range(4))
    interrupted = []
    downstream = AsyncMock()
    monkeypatch.setattr(agent, "process_long_request", downstream)

    async def intent(_text):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            try:
                await release_cleanup.wait()
                cleanup_complete.set()
            except asyncio.CancelledError:
                interrupted.append(True)
                raise

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    update = ownership.update("synthetic intent")
    owner = ownership.launch(update)
    try:
        await started.wait()
        assert state.get_user_state(11).is_processing
        update.effective_message.reply_text.assert_not_awaited()
        owner.cancel("original admission cancellation")
        await cleanup_started.wait()
        if repeat_cancel:
            owner.cancel("later cancellation")
            await settle()
        assert not interrupted, "repeated owner cancellation interrupted intent cleanup"
        assert not owner.done(), "handler returned before its intent cleanup completed"
        assert state.get_user_state(11).is_processing
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("original admission cancellation",)
        assert cleanup_complete.is_set()
        update.effective_message.reply_text.assert_not_awaited()
        downstream.assert_not_awaited()
        assert not state.get_user_state(11).is_processing, "cancelled intent leaked its busy reservation"
        assert not state.get_user_lock(11).locked()
        assert 11 not in state._ACTIVE_TASKS
        assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
        monkeypatch.setattr("app.intent_router.try_direct_intent", AsyncMock(return_value=None))
        await ownership.launch(ownership.update("next admission"))
        await settle()
        downstream.assert_awaited_once()
        assert downstream.await_args.kwargs["text_override"] == "next admission"
        assert not state.get_user_state(11).is_processing
        assert 11 not in state._ACTIVE_TASKS
    finally:
        release_cleanup.set()


@pytest.mark.parametrize("outcome", ["handled", "context", "none", "unhandled", "error"])
async def test_intent_success_and_fallback_semantics_remain(monkeypatch, ownership, outcome):
    result = {
        "handled": SimpleNamespace(handled=True, text="Synthetic direct card", context_data=""),
        "context": SimpleNamespace(handled=False, text="", context_data="synthetic live data"),
        "none": None,
        "unhandled": SimpleNamespace(handled=False, text="", context_data=""),
    }
    intent = (
        AsyncMock(side_effect=RuntimeError("synthetic API error"))
        if outcome == "error"
        else AsyncMock(return_value=result[outcome])
    )
    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    downstream = AsyncMock()
    monkeypatch.setattr(agent, "process_long_request", downstream)
    update = ownership.update("synthetic question")
    await ownership.launch(update)
    await settle()
    intent.assert_awaited_once_with("synthetic question")
    if outcome == "handled":
        update.effective_message.reply_text.assert_awaited_once_with("Synthetic direct card", parse_mode="Markdown")
        downstream.assert_not_awaited()
        assert state.get_last_sent_message(11) == "synthetic question"
    else:
        downstream.assert_awaited_once()
        expected = "synthetic question"
        if outcome == "context":
            expected += (
                "\n\n<live_data>\nsynthetic live data\n</live_data>\n"
                "Используй эти актуальные данные для формирования полноценного ответа."
            )
        assert downstream.await_args.kwargs["text_override"] == expected
    assert not state.get_user_state(11).is_processing
    assert not state.get_user_lock(11).locked()
    assert 11 not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS


async def test_direct_intent_counts_once_without_a_chat_stage(monkeypatch, ownership):
    from app.metrics import MetricsCollector

    collector = MetricsCollector()
    monkeypatch.setattr(messages, "metrics_collector", collector)
    monkeypatch.setattr(
        "app.intent_router.try_direct_intent",
        AsyncMock(return_value=SimpleNamespace(handled=True, text="Synthetic direct card")),
    )
    await ownership.launch(ownership.update("synthetic weather question"))
    await settle()
    collector._drain_events()
    assert collector.metrics.request_count == 1
    assert collector.metrics.api_calls == {}
    assert sum(day[11]["request_count"] for day in collector._user_daily.values()) == 1


async def test_edit_drains_normal_intent_admission_before_starting_new_work(monkeypatch, ownership):
    started, cleanup_started, release_cleanup, cleanup_complete = (asyncio.Event() for _ in range(4))
    replacement_started = asyncio.Event()
    calls = []

    async def intent(_text):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_complete.set()

    async def process(_placeholder, _update, _context, *, text_override, **_kwargs):
        calls.append(text_override)
        replacement_started.set()
        await asyncio.Future()

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    monkeypatch.setattr(agent, "process_long_request", process)
    original = ownership.update("original normal intent")
    owner = ownership.launch(original)
    try:
        await started.wait()
        edit = ownership.launch(ownership.update("replacement text", edited=True))
        await cleanup_started.wait()
        await settle()
        assert state.get_user_state(11).is_processing
        active = state.get_active_task(11)
        assert active is not None and active is not owner and not active.done()
        assert not replacement_started.is_set()
        original.effective_message.reply_text.assert_not_awaited()
        assert not heartbeat._HEARTBEAT_EVENTS
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await edit
        await replacement_started.wait()
        assert cleanup_complete.is_set()
        assert calls == ["replacement text"]
        assert state.get_user_state(11).is_processing
        worker = state.get_active_task(11)
        assert worker is not None and not worker.done()
        assert heartbeat.is_heartbeat_active(ownership.placeholders[11].message_id)
        assert state.cancel_active_task(11)
        await worker
        assert 11 not in state._ACTIVE_TASKS
        assert not state.get_user_state(11).is_processing
        assert not heartbeat._HEARTBEAT_EVENTS
    finally:
        release_cleanup.set()


@pytest.mark.parametrize("completion_first", [False, True])
async def test_intent_completion_racing_caller_cancellation_stays_owned(monkeypatch, ownership, completion_first):
    started, release, completed = (asyncio.Event() for _ in range(3))
    downstream = AsyncMock()

    async def intent(_text):
        started.set()
        try:
            await release.wait()
        finally:
            completed.set()

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    monkeypatch.setattr(agent, "process_long_request", downstream)
    update = ownership.update("race intent")
    owner = ownership.launch(update)
    try:
        await started.wait()
        if completion_first:
            release.set()
            await completed.wait()
        owner.cancel("race cancellation")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("race cancellation",)
        assert completed.is_set()
        downstream.assert_not_awaited()
        update.effective_message.reply_text.assert_not_awaited()
        assert not state.get_user_state(11).is_processing
        assert 11 not in state._ACTIVE_TASKS
        assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
    finally:
        release.set()


async def test_self_cancelled_intent_propagates_cancellation_after_cleanup(monkeypatch, ownership):
    cleanup_complete = asyncio.Event()
    downstream = AsyncMock()

    async def intent(_text):
        try:
            raise asyncio.CancelledError("intent self cancellation")
        finally:
            cleanup_complete.set()

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    monkeypatch.setattr(agent, "process_long_request", downstream)
    update = ownership.update("self cancelled intent")
    with pytest.raises(asyncio.CancelledError):
        await ownership.launch(update)
    assert cleanup_complete.is_set()
    downstream.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS

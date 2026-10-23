"""The generic stop button owns only its current request and placeholder."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import agent, callbacks, messages
from app.i18n import t
from app.utils import heartbeat
from tests import test_edited_request_ownership as ownership_support

pytestmark = pytest.mark.asyncio
ownership = ownership_support.ownership


def _query(*, message_id=1100, chat_id=11, answer=None):
    return SimpleNamespace(
        data="cancel_generation",
        message=SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=chat_id)),
        from_user=SimpleNamespace(id=11, language_code="en"),
        answer=answer or AsyncMock(),
        edit_message_text=AsyncMock(),
    )


@pytest.fixture
async def pending(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=8))
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_LAST_BOT_MESSAGE", {})
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_EVENTS", {})
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_OWNERS", {})
    tasks = []

    async def work():
        await asyncio.Future()

    def start():
        task = asyncio.create_task(work())
        tasks.append(task)
        state.register_active_task(11, task)
        state.set_last_bot_message(11, 1100, 11)
        state.get_user_state(11).is_processing = True
        heartbeat.register_heartbeat(1100, asyncio.Event())
        task.add_done_callback(lambda done: messages._release_admission(11, done))
        return task

    try:
        yield start
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("stale", ["message", "chat", "done", "missing"])
async def test_stale_stop_does_not_cancel_current_work_or_edit_a_final_response(pending, stale):
    owner = pending()
    await asyncio.sleep(0)
    if stale == "done":
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
    elif stale == "missing":
        state.clear_active_task(11)
    query = _query(message_id=999 if stale == "message" else 1100, chat_id=22 if stale == "chat" else 11)
    await callbacks._cancel_generation_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), None
    )
    if stale not in {"done", "missing"}:
        assert not owner.cancelling()
    query.answer.assert_awaited_once_with(t("processing.long_wait_finished", "en"))
    query.edit_message_text.assert_not_awaited()


async def test_old_final_control_cannot_cancel_a_new_preplaceholder_admission(pending):
    owner = pending()
    await asyncio.sleep(0)
    # The previous reply is final; a newer handler owns admission while its
    # replacement placeholder is still being sent, so last_message has not moved.
    heartbeat.stop_heartbeat(1100)
    query = _query()
    await callbacks._cancel_generation_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), None
    )
    assert not owner.cancelling()
    query.answer.assert_awaited_once_with(t("processing.long_wait_finished", "en"))
    query.edit_message_text.assert_not_awaited()


async def test_stop_rechecks_exact_owner_after_answer_transport_yields(pending):
    original = pending()
    replacement = None

    async def replace(*_args, **_kwargs):
        nonlocal replacement
        replacement = pending()

    query = _query(answer=AsyncMock(side_effect=replace))
    await callbacks._cancel_generation_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), None
    )
    assert replacement is not None and not replacement.cancelling()
    assert not original.cancelling()
    assert state.get_active_task(11) is replacement
    query.edit_message_text.assert_not_awaited()


async def test_stop_waits_for_owned_cleanup_before_writing_cancelled_ui(monkeypatch, pending):
    cleanup_started, release, cleaned = (asyncio.Event() for _ in range(3))

    async def work():
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release.wait()
            cleaned.set()

    original = pending()
    original.cancel()
    await asyncio.gather(original, return_exceptions=True)
    worker = asyncio.create_task(work())
    state.register_active_task(11, worker)
    state.get_user_state(11).is_processing = True
    worker.add_done_callback(lambda done: messages._release_admission(11, done))
    await asyncio.sleep(0)
    query = _query()
    callback = asyncio.create_task(
        callbacks._cancel_generation_callback(
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), None
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not callback.done()
        query.edit_message_text.assert_not_awaited()
        assert state.get_user_state(11).is_processing
        release.set()
        await callback
        assert cleaned.is_set() and worker.done()
        query.edit_message_text.assert_awaited_once()
        assert not state.get_user_state(11).is_processing
        assert state.get_active_task(11) is None
    finally:
        release.set()
        callback.cancel()
        worker.cancel()
        await asyncio.gather(callback, worker, return_exceptions=True)


async def test_edit_supersedes_stop_during_drain_without_overwriting_reused_final(monkeypatch, ownership):
    started, cleanup_started, release, replacement_done = (asyncio.Event() for _ in range(4))

    async def process(placeholder, _update, _context, *, text_override, **_kwargs):
        if text_override == "replacement":
            await placeholder.edit_text("Replacement final response")
            replacement_done.set()
            return
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release.wait()

    monkeypatch.setattr(agent, "process_long_request", process)
    await ownership.launch(ownership.update("original"))
    await started.wait()
    query = _query()
    callback = asyncio.create_task(
        callbacks._cancel_generation_callback(
            SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=11)), None
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert state.get_active_task(11) is callback
        edit = ownership.launch(ownership.update("replacement", edited=True))
        await ownership_support.settle()
        assert not replacement_done.is_set()
        release.set()
        await edit
        await asyncio.wait_for(replacement_done.wait(), 1)
        await ownership_support.settle()
        assert callback.cancelled()
        query.edit_message_text.assert_not_awaited()
        assert ownership.ui[-1] == (11, "worker", "Replacement final response")
        assert not state.get_user_state(11).is_processing
        assert state.get_active_task(11) is None
    finally:
        release.set()
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)

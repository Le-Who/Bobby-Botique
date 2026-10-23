"""Mandatory request rejection must produce a terminal UI and release admission."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError

from app import state
from app.handlers import agent, messages
from app.utils import heartbeat
from app.utils.background_tasks import TaskManager
from tests import test_edited_request_ownership as ownership_support
from tests import test_message_admission_failures as admission_support

pytestmark = pytest.mark.asyncio
admission = admission_support.admission
ownership = ownership_support.ownership


@pytest.mark.parametrize("kind", ["text", "photo", "voice", "draw"])
async def test_full_manager_rejects_request_visibly_and_next_admission_runs(monkeypatch, admission, kind):
    inputs = []

    def submit(coroutine):
        inputs.append(coroutine)
        return admission.manager.try_submit(coroutine)

    monkeypatch.setattr(messages, "try_submit_task", submit, raising=False)
    admission.manager.MAX_TASKS = 0
    placeholder = SimpleNamespace(message_id=202, edit_text=AsyncMock(), delete=AsyncMock())
    owner = await admission.launch(kind, 1, AsyncMock(return_value=placeholder))
    await owner
    await asyncio.sleep(0)

    assert not admission.calls
    placeholder.edit_text.assert_awaited_once()
    text = placeholder.edit_text.await_args.args[0].lower()
    assert "overloaded" in text or "перегружен" in text
    markup = placeholder.edit_text.await_args.kwargs.get("reply_markup")
    if kind == "text":
        assert "retry_last" in [button.callback_data for row in markup.inline_keyboard for button in row]
    else:
        assert markup is None, "Media/draw rejection must not retry unrelated prior chat text."
    assert inputs and all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in inputs)
    assert not state.get_user_state(admission.uid).is_processing
    assert not state.get_user_lock(admission.uid).locked()
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS

    admission.manager.MAX_TASKS = 50
    retry = await admission.launch(kind, 2, AsyncMock(return_value=placeholder))
    await retry
    await asyncio.wait_for(admission.started.wait(), 1)
    expected = "voice" if kind == "voice" else "draw" if kind == "draw" else "chat"
    assert admission.calls == [(expected, 2)]
    admission.release.set()
    assert await admission.manager.drain(timeout=1)
    assert not state.get_user_state(admission.uid).is_processing
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS


@pytest.mark.parametrize("failure", ["error", "cancel"])
@pytest.mark.parametrize("kind", ["text", "photo", "voice", "draw"])
async def test_rejection_ui_failure_still_releases_every_admission_resource(monkeypatch, admission, failure, kind):
    monkeypatch.setattr(messages, "try_submit_task", admission.manager.try_submit, raising=False)
    admission.manager.MAX_TASKS = 0
    entered = asyncio.Event()

    async def fail(*_args, **_kwargs):
        entered.set()
        if failure == "error":
            raise NetworkError("Synthetic overload notice failure")
        await asyncio.Future()

    placeholder = SimpleNamespace(message_id=202, edit_text=fail, delete=AsyncMock())
    owner = await admission.launch(kind, 1, AsyncMock(return_value=placeholder))
    await asyncio.wait_for(entered.wait(), 1)
    if failure == "cancel":
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    else:
        with pytest.raises(NetworkError):
            await owner
    assert not admission.calls
    assert not state.get_user_state(admission.uid).is_processing
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS


async def test_edited_request_capacity_rejection_preserves_retry_text_and_closes_input(monkeypatch, ownership):
    manager = TaskManager()
    manager.MAX_TASKS = 0
    inputs = []

    def submit(coroutine):
        inputs.append(coroutine)
        return manager.try_submit(coroutine)

    monkeypatch.setattr(messages, "try_submit_task", submit, raising=False)
    downstream = AsyncMock()
    monkeypatch.setattr(agent, "process_long_request", downstream)
    await ownership.launch(ownership.update("replacement request", edited=True))
    await ownership_support.settle()
    downstream.assert_not_awaited()
    assert inputs and all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in inputs)
    assert state.get_last_sent_message(11) == "replacement request"
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS
    final = ownership.placeholders[11].edit_text.await_args
    assert "overloaded" in final.args[0].lower()
    assert "retry_last" in [
        button.callback_data for row in final.kwargs["reply_markup"].inline_keyboard for button in row
    ]


async def test_rejected_notice_cleanup_cannot_release_a_superseding_edit(monkeypatch, ownership):
    manager = TaskManager()
    manager.MAX_TASKS = 0
    accepted_submit = messages.submit_task
    rejected = False

    def submit(coroutine):
        nonlocal rejected
        if not rejected:
            rejected = True
            return manager.try_submit(coroutine)
        return accepted_submit(coroutine)

    monkeypatch.setattr(messages, "try_submit_task", submit)
    notice_started, cleanup_started, release_cleanup = (asyncio.Event() for _ in range(3))
    replacement_started, release_work = (asyncio.Event() for _ in range(2))
    ownership.context()
    placeholder = ownership.placeholders[11]
    edit = placeholder.edit_text

    async def notice(text, **kwargs):
        if "overloaded" not in text.lower():
            return await edit(text, **kwargs)
        assert not heartbeat.is_heartbeat_active(placeholder.message_id)
        notice_started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    async def process(*_args, **_kwargs):
        replacement_started.set()
        await release_work.wait()

    monkeypatch.setattr(placeholder, "edit_text", notice)
    monkeypatch.setattr(agent, "process_long_request", process)
    original = ownership.launch(ownership.update("original request"))
    try:
        await asyncio.wait_for(notice_started.wait(), 1)
        replacement = ownership.launch(ownership.update("replacement", edited=True))
        await asyncio.wait_for(cleanup_started.wait(), 1)
        admission_owner = state.get_active_task(11)
        assert admission_owner is not None and admission_owner is not original
        assert state.get_user_state(11).is_processing
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await original
        await replacement
        await asyncio.wait_for(replacement_started.wait(), 1)
        assert state.get_user_state(11).is_processing
        assert heartbeat.is_heartbeat_active(placeholder.message_id)
    finally:
        release_cleanup.set()
        release_work.set()


@pytest.mark.parametrize("kind", ["voice", "draw"])
async def test_media_worker_cancelled_before_first_step_releases_admission(monkeypatch, admission, kind):
    inputs = []

    def submit(coroutine):
        inputs.append(coroutine)
        task = admission.manager.try_submit(coroutine)
        assert task is not None
        task.cancel()
        return task

    monkeypatch.setattr(messages, "try_submit_task", submit)
    placeholder = SimpleNamespace(message_id=202, edit_text=AsyncMock(), delete=AsyncMock())
    owner = await admission.launch(kind, 1, AsyncMock(return_value=placeholder))
    await owner
    assert await admission.manager.drain(timeout=1)
    await asyncio.sleep(0)
    assert not admission.calls
    assert inputs and all(inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED for coro in inputs)
    assert not state.get_user_state(admission.uid).is_processing
    assert admission.uid not in state._ACTIVE_TASKS
    assert not heartbeat._HEARTBEAT_EVENTS and not heartbeat._HEARTBEAT_TASKS

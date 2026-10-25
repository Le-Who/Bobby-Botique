"""Suggestion callbacks use the same durable-state and mandatory-work admission."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_smart_actions, messages
from app.utils import heartbeat
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def suggestion(monkeypatch):
    manager = TaskManager()
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=2))
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_schedule_persist", lambda current: None)
    monkeypatch.setattr(
        "app.repos.users.load_user_state",
        AsyncMock(return_value={"role_diaries": {"role": ["saved note"]}, "tarot_mode": True}),
    )
    monkeypatch.setattr("app.utils.background_tasks.submit_task", manager.submit)
    monkeypatch.setattr("app.utils.background_tasks.try_submit_task", manager.try_submit)
    submitted = []

    def submit(coroutine, **kwargs):
        submitted.append(coroutine)
        return manager.try_submit(coroutine, **kwargs)

    monkeypatch.setattr(messages, "try_submit_task", submit)
    monkeypatch.setattr("app.handlers.agent.process_long_request", AsyncMock())
    monkeypatch.setattr("app.metrics.metrics_collector.record_request", AsyncMock())
    for name in ("_HEARTBEAT_EVENTS", "_HEARTBEAT_TASKS", "_HEARTBEAT_OWNERS", "_WAIT_NOTICE_TASKS"):
        monkeypatch.setattr(heartbeat, name, {})
    heartbeat_tasks = []
    register = heartbeat.register_heartbeat

    def register_owned(*args, **kwargs):
        register(*args, **kwargs)
        heartbeat_tasks.extend(heartbeat._HEARTBEAT_TASKS.values())

    monkeypatch.setattr(heartbeat, "register_heartbeat", register_owned)
    placeholder = SimpleNamespace(message_id=91, edit_text=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(return_value=placeholder))
    user = SimpleNamespace(id=42)
    chat = SimpleNamespace(id=43, send_action=AsyncMock())
    query = SimpleNamespace(data="suggest:Explain this", answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=user, effective_chat=chat)
    try:
        yield SimpleNamespace(
            manager=manager,
            placeholder=placeholder,
            update=update,
            context=SimpleNamespace(bot=bot),
            submitted=submitted,
        )
    finally:
        await manager.drain(timeout=0)
        for task in heartbeat_tasks:
            task.cancel()
        await asyncio.gather(*heartbeat_tasks, return_exceptions=True)


async def test_suggestion_restores_unrelated_saved_state_before_recording_request(suggestion):
    await cb_smart_actions.suggestion_callback(suggestion.update, suggestion.context)
    await suggestion.manager.drain(timeout=1)
    current = state.get_user_state(42)
    assert current._loaded_from_db
    assert current.role_diaries == {"role": ["saved note"]}
    assert current.tarot_mode
    assert current.last_sent_message_text == "Explain this"


async def test_suggestion_capacity_rejection_retires_placeholder_and_owner(suggestion):
    suggestion.manager.MAX_TASKS = 0
    await cb_smart_actions.suggestion_callback(suggestion.update, suggestion.context)
    assert suggestion.placeholder.edit_text.await_count == 1
    assert "перегруж" in suggestion.placeholder.edit_text.await_args.args[0].lower()
    assert not state.get_user_state(42).is_processing
    assert state.get_active_task(42) is None
    assert not heartbeat.is_heartbeat_active(91, chat_id=43)
    assert len(suggestion.submitted) == 1
    assert inspect.getcoroutinestate(suggestion.submitted[0]) == inspect.CORO_CLOSED


async def test_suggestion_cancelled_before_worker_starts_releases_admission(suggestion):
    await cb_smart_actions.suggestion_callback(suggestion.update, suggestion.context)
    owner = state.get_active_task(42)
    assert owner is not None
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    assert not state.get_user_state(42).is_processing
    assert state.get_active_task(42) is None
    assert not heartbeat.is_heartbeat_active(91, chat_id=43)

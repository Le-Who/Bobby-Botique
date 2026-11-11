"""Explicit context resets retire previews even after runtime ownership expires."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_roles, commands
from tests.interaction_fakes import install_private_interaction_fakes


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    install_private_interaction_fakes(monkeypatch)


def saved():
    return {
        "document_mode": True,
        "selected_document_id": 19,
        "role_diaries": {"role": ["Synthetic diary"]},
        "generated_role": {
            "title": "Persisted role",
            "prompt": "Synthetic prompt",
            "_interaction_token": "a" * 16,
            "_memory_epoch": 7,
        },
        "awaiting_custom_role_input": True,
        "last_custom_role_prompt": "Old input",
        "generating_custom_role": True,
        "awaiting_manual_role_title": False,
        "awaiting_manual_role_prompt": True,
        "manual_role_title": "Manual title",
        "manual_role_prompt": "Manual prompt",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("warm", [False, True])
async def test_newchat_retires_persisted_preview_without_runtime_owner(monkeypatch, warm):
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=saved()))
    if warm:
        await state.ensure_state_loaded(811)
    assert state.get_user_state(811).interaction is None
    user = SimpleNamespace(id=811)
    message = SimpleNamespace(text="/newchat", from_user=user, chat=SimpleNamespace(id=811), reply_text=AsyncMock())
    update = SimpleNamespace(
        message=message,
        effective_message=message,
        effective_user=user,
        effective_chat=message.chat,
        callback_query=None,
    )
    chat = SimpleNamespace(history=[], token_count=0, system_prompt=None)
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(commands, "update_user_chat", AsyncMock(return_value=True))
    await commands.new_chat_command(update, SimpleNamespace(user_data={}))
    writer = AsyncMock(return_value=True)
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    query = SimpleNamespace(
        from_user=user, data="role_custom_apply:" + "a" * 16, answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    await cb_roles.role_custom_apply_callback(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}))
    writer.assert_not_awaited()
    current = state.get_user_state(811)
    assert current.generated_role is None
    assert not current.awaiting_custom_role_input
    assert not current.awaiting_manual_role_prompt
    assert current.selected_document_id == 19
    assert current.role_diaries == {"role": ["Synthetic diary"]}


@pytest.mark.asyncio
async def test_cold_retirement_hydrates_before_persisting_other_fields(monkeypatch):
    loader = AsyncMock(return_value=saved())
    saver = AsyncMock()
    monkeypatch.setattr("app.repos.users.load_user_state", loader)
    monkeypatch.setattr("app.repos.users.save_user_state", saver)
    current = state.get_user_state(811)
    async with state.get_user_lock(811):
        await state.cancel_interaction(811)
    assert await state._persist(current)
    loader.assert_awaited_once_with(811)
    values = saver.await_args.kwargs
    assert values["generated_role"] is None
    assert not values["awaiting_manual_role_prompt"]
    assert values["selected_document_id"] == 19
    assert values["role_diaries"] == {"role": ["Synthetic diary"]}


@pytest.mark.asyncio
async def test_manual_stage_recovery_still_keeps_persisted_input(monkeypatch):
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=saved()))
    current = await state.ensure_state_loaded(811)
    state.begin_interaction(811, "manual_role")
    assert current.awaiting_manual_role_prompt
    assert current.manual_role_title == "Manual title"
    assert current.manual_role_prompt == "Manual prompt"

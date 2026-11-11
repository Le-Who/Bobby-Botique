import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import cb_branches, cb_conversations, cb_documents, cb_models, commands


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reset", "prompt", "model", "conversation", "document", "branch"])
async def test_explicit_context_change_drains_running_interaction(monkeypatch, action):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=None))
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    uid = 811
    owner = state.begin_interaction(uid, "role")
    entered, cleanup = asyncio.Event(), asyncio.Event()

    async def generate():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup.set()

    task = asyncio.create_task(state.run_interaction(owner, generate))
    await asyncio.wait_for(entered.wait(), 1)
    message = MagicMock(reply_text=AsyncMock())
    query = MagicMock(from_user=SimpleNamespace(id=uid), message=message)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=uid, type="private"),
        callback_query=query,
        message=message,
    )
    context = SimpleNamespace(user_data={})
    chat = SimpleNamespace(history=[], token_count=0, system_prompt=None, branch_id=None)

    async def persist(*args, **kwargs):
        assert cleanup.is_set(), "context write overtook the previous interaction"
        assert state.get_user_state(uid).interaction is None
        return True

    async def leave(*args):
        pass

    if action in {"reset", "prompt"}:
        monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=chat))
        monkeypatch.setattr(commands, "update_user_chat", persist)
        monkeypatch.setattr(commands, "leave_tarot_for_chat", leave)
        context.args = ["Synthetic", "prompt"]
        run = (
            commands.new_chat_command(update, context)
            if action == "reset"
            else commands.set_prompt_command(update, context)
        )
    elif action == "model":
        query.data = "switch_model:synthetic-model"
        monkeypatch.setattr(cb_models.menus, "get_admin_selected_chat_model", lambda: None)
        monkeypatch.setattr(cb_models.menus, "get_visible_model_list", lambda *args: ["synthetic-model"])
        monkeypatch.setattr(cb_models, "get_user_chat", AsyncMock(return_value=chat))
        monkeypatch.setattr(cb_models, "update_user_chat", persist)
        monkeypatch.setattr(cb_models, "leave_tarot_for_chat", leave)
        run = cb_models.switch_model_callback(update, context)
    elif action == "conversation":
        query.data = "conv_switch_to:1"
        monkeypatch.setattr(cb_conversations, "switch_to_conversation", persist)
        monkeypatch.setattr(cb_conversations.role_conv_metrics, "record_conversation_switched", AsyncMock())
        monkeypatch.setattr(
            cb_conversations.menus, "get_conversations_menu_content", AsyncMock(return_value=("ok", None, None))
        )
        run = cb_conversations.conv_switch_to_callback(update, context)
    elif action == "document":
        query.data = "doc:select:1"
        state.set_document_mode(uid, True, 1)
        monkeypatch.setattr(
            cb_documents,
            "get_document_by_id",
            AsyncMock(return_value={"filename": "test", "pages": 1, "created_at": "2026-11-11"}),
        )
        original = cb_documents.set_document_mode

        def select(*args):
            assert cleanup.is_set(), "document selection left an old interaction running"
            original(*args)

        monkeypatch.setattr(cb_documents, "set_document_mode", select)
        run = cb_documents.document_callback(update, context)
    else:
        monkeypatch.setattr(cb_branches, "get_user_chat", AsyncMock(return_value=chat))
        monkeypatch.setattr(cb_branches, "create_branch", AsyncMock(return_value=1))
        monkeypatch.setattr(cb_branches, "update_user_chat", persist)
        run = cb_branches.branch_create_callback(update, context)
    try:
        await run
        assert cleanup.is_set()
    finally:
        await state.cancel_interaction(uid)
        await task

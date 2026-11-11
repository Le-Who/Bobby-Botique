"""The latest explicit role, canvas or rename input owns the next message."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import cb_conversations, cb_image, cb_roles, commands, messages, msg_roles, tarot_chat
from tests.factories import make_chat_state
from tests.interaction_fakes import install_private_interaction_fakes


@pytest.fixture
def input_boundary(monkeypatch):
    uid = 913
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    state.get_user_state(uid)._loaded_from_db = True
    install_private_interaction_fakes(monkeypatch, chat_reader=True)
    monkeypatch.setattr(messages, "is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "check_user_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(cb_roles, "_is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "process_media_group_update", AsyncMock(return_value=False))
    monkeypatch.setattr(messages, "handle_document_mode_interaction", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.board_handler.try_handle_board_reply", AsyncMock(return_value=False))
    monkeypatch.setattr("app.middleware.dedup.is_duplicate_request", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.msg_speech.try_handle_speech_collection", AsyncMock(return_value=False))
    monkeypatch.setattr(msg_roles, "get_user_chat", AsyncMock(return_value=make_chat_state()))
    monkeypatch.setattr("app.process_policies.resolve_process", AsyncMock(return_value=SimpleNamespace(explicit=True)))
    provider = AsyncMock(return_value=('{"title":"New role","purpose":"Purpose","prompt":"Prompt"}', 1))
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    rename_conversation = AsyncMock(return_value=True)
    rename_role = AsyncMock(return_value=True)
    monkeypatch.setattr(msg_roles, "rename_conversation", rename_conversation)
    monkeypatch.setattr("app.repos.roles.rename_custom_role", rename_role)
    monkeypatch.setattr(msg_roles.role_conv_metrics, "record_conversation_renamed", AsyncMock())
    monkeypatch.setattr(
        msg_roles.menus, "get_conversations_menu_content", AsyncMock(return_value=("Conversations", None, None))
    )
    monkeypatch.setattr(msg_roles.menus, "get_roles_menu_content", AsyncMock(return_value=("Roles", None, None)))
    monkeypatch.setattr(messages, "api_logger", MagicMock())
    monkeypatch.setattr(messages, "metrics_collector", SimpleNamespace(record_request=AsyncMock()))

    def ignore_background(coroutine, **kwargs):
        coroutine.close()

    monkeypatch.setattr(messages, "submit_task", ignore_background)
    user = SimpleNamespace(id=uid, language_code="ru")
    chat = SimpleNamespace(id=uid, type="private")
    progress = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    message = SimpleNamespace(
        text="Synthetic new input",
        message_id=81,
        from_user=user,
        chat=chat,
        chat_id=uid,
        reply_text=AsyncMock(return_value=progress),
        delete=AsyncMock(),
        caption=None,
        photo=(),
        voice=None,
        document=None,
        reply_to_message=None,
    )
    query = SimpleNamespace(
        id="synthetic-input",
        from_user=user,
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        data="",
    )
    incoming = SimpleNamespace(
        update_id=81,
        message=message,
        effective_message=message,
        effective_user=user,
        effective_chat=chat,
        callback_query=None,
    )
    callback = SimpleNamespace(callback_query=query, effective_user=user, effective_chat=chat)
    context = SimpleNamespace(
        user_data={
            "draw_state": {
                "prompt": "Old image",
                "model": "zimage",
                "aspect_ratio": "1:1",
                "enhance_prompt": False,
                "awaiting_prompt": False,
                "last_photo_msg": None,
            }
        },
        bot=SimpleNamespace(id=1, username="synthetic"),
    )
    return SimpleNamespace(
        uid=uid,
        incoming=incoming,
        callback=callback,
        query=query,
        context=context,
        provider=provider,
        rename_conversation=rename_conversation,
        rename_role=rename_role,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", ["canvas", "conversation_rename", "role_rename"])
async def test_role_entry_retires_previous_local_text_input(input_boundary, previous):
    boundary = input_boundary
    data = boundary.context.user_data
    if previous == "canvas":
        boundary.query.data = "draw:edit:prompt"
        await cb_image.draw_callback(boundary.callback, boundary.context)
    elif previous == "conversation_rename":
        boundary.query.data = "conv_rename_ask:19"
        await cb_conversations.conv_rename_ask_callback(boundary.callback, boundary.context)
    else:
        boundary.query.data = "role_rename_pick:19"
        await cb_roles.role_rename_pick_callback(boundary.callback, boundary.context)
    await cb_roles.role_create_callback(boundary.callback, boundary.context)
    await messages.handle_request(boundary.incoming, boundary.context)
    boundary.provider.assert_awaited_once()
    boundary.rename_conversation.assert_not_awaited()
    boundary.rename_role.assert_not_awaited()
    assert state.get_generated_role(boundary.uid)["title"] == "New role"
    assert not data["draw_state"]["awaiting_prompt"]


@pytest.mark.asyncio
async def test_canvas_edit_leaves_tarot_before_capturing_next_text(input_boundary, monkeypatch):
    boundary = input_boundary
    state.set_tarot_mode(boundary.uid, True)
    state.set_tarot_session(boundary.uid, {"history": [], "last_activity_at": 1})
    tarot = AsyncMock()
    monkeypatch.setattr(tarot_chat, "handle_tarot_message", tarot)
    boundary.query.data = "draw:edit:prompt"
    await cb_image.draw_callback(boundary.callback, boundary.context)
    await messages.handle_request(boundary.incoming, boundary.context)
    tarot.assert_not_awaited()
    assert not state.is_in_tarot_mode(boundary.uid)
    assert boundary.context.user_data["draw_state"]["prompt"] == "Synthetic new input"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_mode", ["canvas", "conversation_rename", "role_rename"])
async def test_new_local_input_retires_previous_role_creation(input_boundary, new_mode):
    boundary = input_boundary
    await cb_roles.role_create_callback(boundary.callback, boundary.context)
    boundary.query.data = {
        "canvas": "draw:edit:prompt",
        "conversation_rename": "conv_rename_ask:19",
        "role_rename": "role_rename_pick:19",
    }[new_mode]
    handler = {
        "canvas": cb_image.draw_callback,
        "conversation_rename": cb_conversations.conv_rename_ask_callback,
        "role_rename": cb_roles.role_rename_pick_callback,
    }[new_mode]
    await handler(boundary.callback, boundary.context)
    await messages.handle_request(boundary.incoming, boundary.context)
    boundary.provider.assert_not_awaited()
    assert not state.is_awaiting_custom_role_input(boundary.uid)
    if new_mode == "canvas":
        assert boundary.context.user_data["draw_state"]["prompt"] == "Synthetic new input"
    elif new_mode == "conversation_rename":
        boundary.rename_conversation.assert_awaited_once()
    else:
        boundary.rename_role.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["canvas", "conversation_rename", "role_rename"])
@pytest.mark.parametrize("reset", ["newchat", "miniapp"])
async def test_context_reset_retires_local_draft_before_next_text(input_boundary, monkeypatch, mode, reset):
    boundary = input_boundary
    boundary.context.user_data["model_list"] = ["Synthetic cached model"]
    boundary.context.user_data["unrelated_setting"] = "keep"
    boundary.query.data = {
        "canvas": "draw:edit:prompt",
        "conversation_rename": "conv_rename_ask:19",
        "role_rename": "role_rename_pick:19",
    }[mode]
    handler = {
        "canvas": cb_image.draw_callback,
        "conversation_rename": cb_conversations.conv_rename_ask_callback,
        "role_rename": cb_roles.role_rename_pick_callback,
    }[mode]
    await handler(boundary.callback, boundary.context)
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=make_chat_state()))
    monkeypatch.setattr(commands, "update_user_chat", AsyncMock(return_value=True))
    if reset == "newchat":
        await commands.new_chat_command(boundary.incoming, boundary.context)
    else:
        from quart import Quart

        from app import web_miniapp

        monkeypatch.setattr(web_miniapp, "_validate_init_data", lambda *args: {"user": {"id": boundary.uid}})
        monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
        monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=make_chat_state()))
        monkeypatch.setattr("app.repos.chats.update_user_chat", AsyncMock(return_value=True))
        app = Quart(__name__)
        app.register_blueprint(web_miniapp.miniapp_blueprint, url_prefix="/webapp")
        response = await app.test_client().post("/webapp/api/context/reset", headers={"Authorization": "tma synthetic"})
        assert response.status_code == 200
    downstream = AsyncMock(return_value=True)
    monkeypatch.setattr(messages, "handle_document_mode_interaction", downstream)
    await messages.handle_request(boundary.incoming, boundary.context)
    downstream.assert_awaited_once()
    boundary.rename_conversation.assert_not_awaited()
    boundary.rename_role.assert_not_awaited()
    data = boundary.context.user_data
    assert not data["draw_state"]["awaiting_prompt"]
    assert data["draw_state"]["model"] == "zimage"
    assert data["model_list"] == ["Synthetic cached model"]
    assert data["unrelated_setting"] == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["conversation_rename", "role_rename"])
async def test_failed_rename_keeps_owned_input_and_never_reports_success(input_boundary, mode):
    boundary = input_boundary
    boundary.query.data = "conv_rename_ask:19" if mode == "conversation_rename" else "role_rename_pick:19"
    handler = (
        cb_conversations.conv_rename_ask_callback
        if mode == "conversation_rename"
        else cb_roles.role_rename_pick_callback
    )
    await handler(boundary.callback, boundary.context)
    writer = boundary.rename_conversation if mode == "conversation_rename" else boundary.rename_role
    writer.return_value = False
    boundary.incoming.message.reply_text.reset_mock()
    await messages.handle_request(boundary.incoming, boundary.context)
    key = "rename_conv_id" if mode == "conversation_rename" else "rename_role_id"
    assert boundary.context.user_data[key] == 19
    replies = [call.args[0] for call in boundary.incoming.message.reply_text.await_args_list]
    assert replies
    assert all(not text.startswith("✅") for text in replies)
    msg_roles.role_conv_metrics.record_conversation_renamed.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_rename_repository_reports_a_missing_row(monkeypatch):
    from app.repos import roles

    monkeypatch.setattr(roles.db, "db_query", AsyncMock(return_value=[]))
    assert await roles.rename_custom_role(19, 913, "Synthetic title") is False


@pytest.mark.asyncio
async def test_canvas_edit_waits_for_previous_provider_cleanup(input_boundary):
    boundary = input_boundary
    await cb_roles.role_create_callback(boundary.callback, boundary.context)
    owner = state.get_user_state(boundary.uid).interaction
    entered, cleaning, release = (asyncio.Event() for _ in range(3))

    async def generate():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    old = asyncio.create_task(state.run_interaction(owner, generate))
    new = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        boundary.query.data = "draw:edit:prompt"
        new = asyncio.create_task(cb_image.draw_callback(boundary.callback, boundary.context))
        await asyncio.wait_for(cleaning.wait(), 1)
        boundary.query.edit_message_caption.assert_not_awaited()
        assert not new.done()
    finally:
        release.set()
        await asyncio.gather(old, *([new] if new is not None else []))
    assert boundary.context.user_data["draw_state"]["awaiting_prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["conversation_rename", "role_rename"])
async def test_old_rename_ack_cannot_clear_or_publish_over_new_input(input_boundary, monkeypatch, mode):
    boundary = input_boundary
    prefix = "conv_rename_ask" if mode == "conversation_rename" else "role_rename_pick"
    handler = (
        cb_conversations.conv_rename_ask_callback
        if mode == "conversation_rename"
        else cb_roles.role_rename_pick_callback
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def rename(*args):
        started.set()
        await release.wait()
        return True

    if mode == "conversation_rename":
        monkeypatch.setattr(msg_roles, "rename_conversation", rename)
    else:
        monkeypatch.setattr("app.repos.roles.rename_custom_role", rename)
    boundary.query.data = f"{prefix}:19"
    await handler(boundary.callback, boundary.context)
    work = asyncio.create_task(messages.handle_request(boundary.incoming, boundary.context))
    try:
        await asyncio.wait_for(started.wait(), 1)
        boundary.query.data = f"{prefix}:20"
        await handler(boundary.callback, boundary.context)
    finally:
        release.set()
        await work
    key = "rename_conv_id" if mode == "conversation_rename" else "rename_role_id"
    assert boundary.context.user_data[key] == 20
    boundary.incoming.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_tarot_entry_retires_legacy_canvas_and_rename_drafts(input_boundary):
    from app.handlers.cmd_tarot import tarot_command

    boundary = input_boundary
    boundary.context.user_data.update(rename_conv_id=19, model_list=["Keep model cache"])
    boundary.context.user_data["draw_state"]["awaiting_prompt"] = True
    boundary.context.bot.send_message = AsyncMock()
    assert await tarot_command(boundary.incoming, boundary.context)
    assert state.is_in_tarot_mode(boundary.uid)
    assert "rename_conv_id" not in boundary.context.user_data
    assert not boundary.context.user_data["draw_state"]["awaiting_prompt"]
    assert boundary.context.user_data["model_list"] == ["Keep model cache"]


@pytest.mark.asyncio
@pytest.mark.parametrize("held_at", ["keyboard", "callback_answer"])
async def test_older_role_entry_cannot_retire_newer_canvas_input(input_boundary, held_at):
    boundary = input_boundary
    entered, release = asyncio.Event(), asyncio.Event()
    older_query = SimpleNamespace(**vars(boundary.query))
    older_update = SimpleNamespace(callback_query=older_query)
    if held_at == "keyboard":
        state.set_tarot_mode(boundary.uid, True)
        state.set_tarot_session(boundary.uid, {"history": [], "last_activity_at": 1})

        async def reply(*args, **kwargs):
            entered.set()
            await release.wait()

        boundary.incoming.message.reply_text.side_effect = reply
    else:

        async def answer(*args, **kwargs):
            entered.set()
            await release.wait()

        older_query.answer = answer
    older = asyncio.create_task(cb_roles.role_create_callback(older_update, boundary.context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        boundary.query.data = "draw:edit:prompt"
        await cb_image.draw_callback(boundary.callback, boundary.context)
        newer_owner = state.get_user_state(boundary.uid).interaction
        assert newer_owner.kind == "draw"
    finally:
        release.set()
        await older
    assert state.get_user_state(boundary.uid).interaction is newer_owner
    assert boundary.context.user_data["draw_state"]["awaiting_prompt"]
    assert not state.is_awaiting_custom_role_input(boundary.uid)
    older_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_newchat_invalidates_role_entry_waiting_for_keyboard_retirement(input_boundary, monkeypatch):
    boundary = input_boundary
    state.set_tarot_mode(boundary.uid, True)
    state.set_tarot_session(boundary.uid, {"history": [], "last_activity_at": 1})
    entered, release = asyncio.Event(), asyncio.Event()

    async def reply(text, **kwargs):
        if "Сеанс Таро завершен" in text:
            entered.set()
            await release.wait()

    boundary.incoming.message.reply_text.side_effect = reply
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=make_chat_state()))
    monkeypatch.setattr(commands, "update_user_chat", AsyncMock(return_value=True))
    older = asyncio.create_task(cb_roles.role_create_callback(boundary.callback, boundary.context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await commands.new_chat_command(boundary.incoming, boundary.context)
    finally:
        release.set()
        await older
    assert state.get_user_state(boundary.uid).interaction is None
    assert not state.is_awaiting_custom_role_input(boundary.uid)
    boundary.query.edit_message_text.assert_not_awaited()

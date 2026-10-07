"""Offline admission and cleanup contracts for real message/callback handlers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import messages
from tests.factories import make_chat_state, make_telegram_context, make_telegram_update


@pytest.fixture
async def request_boundary(monkeypatch):
    user_id = 12345
    user_state = state.get_user_state(user_id)
    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock(return_value=user_state))
    monkeypatch.setattr(state, "_schedule_persist", MagicMock())
    monkeypatch.setattr(messages, "is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "check_user_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(
        messages, "api_logger", MagicMock(log_request=MagicMock(return_value=0), log_response=MagicMock(return_value=0))
    )
    monkeypatch.setattr(messages, "metrics_collector", SimpleNamespace(record_request=AsyncMock()))
    # Select the ordinary text branch without external interceptors or timers.
    for name in (
        "process_media_group_update",
        "handle_role_rename",
        "handle_edit_prompt",
        "handle_conversation_rename",
        "handle_manual_role_input",
        "handle_custom_role_generation",
        "handle_document_mode_interaction",
    ):
        monkeypatch.setattr(messages, name, AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.board_handler.try_handle_board_reply", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.cmd_image.handle_draw_prompt_input", AsyncMock(return_value=False))
    monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", AsyncMock(return_value=None))
    monkeypatch.setattr("app.middleware.dedup.is_duplicate_request", AsyncMock(return_value=False))
    monkeypatch.setattr("app.intent_router.try_direct_intent", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.voice_intent.detect_tts_intent", AsyncMock(return_value=SimpleNamespace(explicit_tts=False))
    )

    async def debounce(_user_id, message, **kwargs):
        return SimpleNamespace(
            build_llm_context=lambda: message.text,
            has_forwarded_content=False,
            forwarded_photo_messages=[],
            user_entries=[],
            forwarded_entries=[],
        )

    monkeypatch.setattr("app.middleware.debounce.debounce_message", debounce)
    busy = AsyncMock()
    monkeypatch.setattr(messages, "_send_busy_ephemeral", busy)
    monkeypatch.setattr(messages, "register_heartbeat", MagicMock())
    monkeypatch.setattr(messages, "unregister_heartbeat", MagicMock())
    monkeypatch.setattr(messages, "stop_heartbeat", MagicMock())
    monkeypatch.setattr(messages, "show_long_wait_retry", AsyncMock())

    owned = []
    registered = []
    register = state.register_active_task

    def submit(coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        owned.append(task)
        return task

    def register_task(uid, task):
        registered.append(task)
        register(uid, task)

    monkeypatch.setattr(messages, "submit_task", submit)
    monkeypatch.setattr(state, "register_active_task", register_task)
    state.clear_network_stall(user_id)
    state.clear_active_task(user_id)
    try:
        yield SimpleNamespace(user_id=user_id, busy=busy, registered=registered)
    finally:
        for task in owned:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
        state.clear_active_task(user_id)
        state.clear_network_stall(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "cancel"])
async def test_request_rejects_busy_user_then_accepts_after_cleanup(monkeypatch, request_boundary, finish):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    third_started = asyncio.Event()
    events = []
    uid = request_boundary.user_id

    async def process(placeholder, update, context, *, text_override, reply_with_voice):
        assert state.get_user_lock(uid).locked()
        assert reply_with_voice is False
        events.append(("start", text_override))
        try:
            if text_override == "first request":
                first_started.set()
                await release_first.wait()
            elif text_override == "third request":
                third_started.set()
            else:
                raise AssertionError("busy second request must not enter the agent")
        finally:
            events.append(("end", text_override))

    monkeypatch.setattr("app.handlers.agent.process_long_request", process)
    context = make_telegram_context()
    updates = [
        make_telegram_update(text, user_id=uid, chat_id=uid)
        for text in ("first request", "second request", "third request")
    ]
    for index, update in enumerate(updates, 1):
        update.update_id = index
        update.effective_message.reply_text.return_value.message_id = 100 + index

    try:
        await messages.handle_request(updates[0], context)
        await asyncio.wait_for(first_started.wait(), 1)
        assert state.get_user_state(uid).is_processing
        assert state.get_user_lock(uid).locked()

        # The second handler returns while the first remains gated, without queueing.
        await asyncio.wait_for(messages.handle_request(updates[1], context), 1)
        request_boundary.busy.assert_awaited_once_with(updates[1])
        assert len(request_boundary.registered) == 1
        assert events == [("start", "first request")]

        if finish == "cancel":
            request_boundary.registered[0].cancel()
        else:
            release_first.set()
        await asyncio.wait_for(request_boundary.registered[0], 1)
        assert not state.get_user_state(uid).is_processing
        assert not state.get_user_lock(uid).locked()
        assert uid not in state._ACTIVE_TASKS

        await messages.handle_request(updates[2], context)
        await asyncio.wait_for(third_started.wait(), 1)
        await asyncio.wait_for(request_boundary.registered[1], 1)
        assert events == [
            ("start", "first request"),
            ("end", "first request"),
            ("start", "third request"),
            ("end", "third request"),
        ]
        assert not state.get_user_state(uid).is_processing
        assert not state.get_user_lock(uid).locked()
        assert uid not in state._ACTIVE_TASKS
    finally:
        release_first.set()


@pytest.mark.asyncio
async def test_new_chat_callback_rejects_busy_user_before_read_or_write(monkeypatch):
    from app.handlers.cb_navigation import new_chat_callback

    uid = 12345
    get_chat = AsyncMock(return_value=make_chat_state())
    update_chat = AsyncMock()
    monkeypatch.setattr("app.handlers.cb_navigation.get_user_chat", get_chat)
    monkeypatch.setattr("app.handlers.cb_navigation.update_user_chat", update_chat)
    update = make_telegram_update("callback", user_id=uid)
    update.callback_query = SimpleNamespace(
        from_user=SimpleNamespace(id=uid),
        data="new_chat",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    lock = state.get_user_lock(uid)
    async with lock:
        await new_chat_callback(update, make_telegram_context())

    update.callback_query.answer.assert_awaited_once()
    assert "Дождитесь" in update.callback_query.answer.await_args.args[0]
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
    get_chat.assert_not_awaited()
    update_chat.assert_not_awaited()
    update.callback_query.edit_message_text.assert_not_awaited()

"""Explicit ordinary-chat actions must end a hydrated Tarot session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import ReplyKeyboardRemove

from app import state
from app.handlers import cb_models, cb_navigation, commands, tarot_chat


@pytest.fixture(autouse=True)
def _offline_persistence(monkeypatch):
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "handler_name", "callback_data"),
    [
        (commands, "start_command", None),
        (commands, "new_chat_command", None),
        (cb_navigation, "new_chat_callback", "new_chat"),
        (cb_navigation, "new_topic_callback", "new_topic"),
        (cb_models, "model_button_callback", "model:0"),
        (cb_models, "switch_model_callback", "switch_model:gemini-3.8-flash"),
    ],
)
async def test_ordinary_chat_action_clears_persisted_tarot(module, handler_name, callback_data):
    user_id = 222010
    user = SimpleNamespace(id=user_id, language_code="ru")
    message = SimpleNamespace(text="/newchat", from_user=user, reply_text=AsyncMock())
    query = SimpleNamespace(
        id="test-callback",
        data=callback_data,
        from_user=user,
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=user,
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        message=None if callback_data else message,
        callback_query=query if callback_data else None,
        update_id=10,
    )
    context = SimpleNamespace(user_data={}, args=[])
    chat_state = SimpleNamespace(history=[], model="gemini-3.8-flash", token_count=0)
    persisted = {"tarot_mode": True, "tarot_session": {"last_activity_at": 1.0}}
    menus = MagicMock()
    menus.get_visible_model_list.return_value = ["gemini-3.8-flash"]
    menus.get_model_menu_content.return_value = ("Модели", "HTML", None)
    menus.get_start_menu_content = AsyncMock(return_value=("Меню", "HTML", None))

    with (
        patch("app.repos.users.load_user_state", new=AsyncMock(return_value=persisted)),
        patch("app.utils.decorators.is_authorized", new=AsyncMock(return_value=True)),
        patch.object(module, "get_user_chat", new=AsyncMock(return_value=chat_state)),
        patch.object(module, "update_user_chat", new=AsyncMock()),
        patch.object(module, "menus", menus),
        patch.object(module, "_is_user_busy", return_value=False, create=True),
    ):
        await getattr(module, handler_name)(update, context)
        # A later hydration must not bring the stale mode back.
        await state.ensure_state_loaded(user_id)

    assert not state.is_in_tarot_mode(user_id)
    assert state.get_tarot_session(user_id) is None
    assert not tarot_chat.is_tarot_mode_filter.filter(message)
    assert any(
        isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove) for call in message.reply_text.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "callback_data", "busy"),
    [
        ("model_button_callback", "model_none", False),
        ("model_button_callback", "model:99", False),
        ("model_button_callback", "model:0", True),
        ("switch_model_callback", "switch_model:unavailable", False),
        ("switch_model_callback", "switch_model:gemini-3.8-flash", True),
    ],
)
async def test_rejected_model_selection_keeps_tarot_session(handler_name, callback_data, busy):
    user_id = 222011
    state.get_user_state(user_id)._loaded_from_db = True
    state.set_tarot_mode(user_id, True)
    state.set_tarot_session(user_id, {"last_activity_at": 1.0})
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=user_id),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    menus = MagicMock()
    menus.get_visible_model_list.return_value = ["gemini-3.8-flash"]

    with (
        patch.object(cb_models, "_is_user_busy", return_value=busy),
        patch.object(cb_models, "menus", menus),
    ):
        await getattr(cb_models, handler_name)(SimpleNamespace(callback_query=query), SimpleNamespace(user_data={}))

    assert state.is_in_tarot_mode(user_id)
    assert state.get_tarot_session(user_id) == {"last_activity_at": 1.0}
    message.reply_text.assert_not_awaited()

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove

from app import state
from app.handlers import messages, tarot_chat


class DummyUser:
    def __init__(self, user_id: int = 123):
        self.id = user_id
        self.username = "testuser"
        self.first_name = "Test"
        self.language_code = "ru"


class DummyChat:
    def __init__(self, chat_id: int = 456):
        self.id = chat_id
        self.type = "private"


class DummyMessage:
    def __init__(self, text: str, user: DummyUser, chat: DummyChat):
        self.message_id = 111
        self.text = text
        self.from_user = user
        self.chat = chat
        self.reply_text = AsyncMock()
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()
        self.photo = ()
        self.document = None
        self.voice = None
        self.caption = None


class DummyUpdate:
    def __init__(self, text: str, user_id: int = 123, chat_id: int = 456):
        user = DummyUser(user_id)
        chat = DummyChat(chat_id)
        self.update_id = 999
        self.message = DummyMessage(text, user, chat)
        self.effective_user = user
        self.effective_chat = chat
        self.effective_message = self.message
        self.callback_query = None


class DummyCallbackQuery:
    def __init__(self, data: str, user: DummyUser, message: DummyMessage):
        self.id = "cb-1"
        self.data = data
        self.from_user = user
        self.message = message
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


class DummyCallbackUpdate:
    def __init__(self, data: str, user_id: int = 123, chat_id: int = 456):
        user = DummyUser(user_id)
        chat = DummyChat(chat_id)
        message = DummyMessage("", user, chat)
        self.update_id = 1000
        self.message = None
        self.callback_query = DummyCallbackQuery(data, user, message)
        self.effective_user = user
        self.effective_chat = chat
        self.effective_message = message


class DummyContext:
    def __init__(self):
        self.bot = MagicMock()
        self.application = MagicMock()
        self.user_data = {}


@pytest.mark.asyncio
async def test_stale_tarot_end_button_removes_reply_keyboard_without_active_mode():
    user_id = 987654
    state.clear_tarot_session(user_id)
    update = DummyUpdate("Завершить сеанс Таро", user_id=user_id)
    context = DummyContext()

    with patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True):
        await tarot_chat.handle_tarot_end_session_message(update, context)

    assert not state.is_in_tarot_mode(user_id)
    assert state.get_tarot_session(user_id) is None
    update.message.reply_text.assert_awaited_once()
    kwargs = update.message.reply_text.await_args.kwargs
    assert isinstance(kwargs["reply_markup"], ReplyKeyboardRemove)


def test_tarot_end_button_text_matches_emoji_and_plain_variants():
    assert tarot_chat.is_tarot_end_session_text("🛑 Завершить сеанс Таро")
    assert tarot_chat.is_tarot_end_session_text("Завершить сеанс Таро")
    assert tarot_chat.is_tarot_end_session_text("  завершить сеанс таро  ")
    assert not tarot_chat.is_tarot_end_session_text("таро")


def test_legacy_tarot_session_without_activity_timestamp_is_idle():
    assert tarot_chat._is_tarot_session_idle({"waiting_for_question": True})


def test_stale_tarot_end_button_handler_is_registered_before_tarot_mode():
    application = MagicMock()

    messages.register(application)

    callbacks = [call.args[0].callback for call in application.add_handler.call_args_list[:2]]
    assert callbacks == [
        tarot_chat.handle_tarot_end_session_message,
        tarot_chat.handle_tarot_message,
    ]


def test_tarot_idle_choice_callback_is_registered_with_tarot_messages():
    application = MagicMock()

    messages.register(application)

    callbacks = [
        call.args[0].callback for call in application.add_handler.call_args_list if hasattr(call.args[0], "callback")
    ]
    assert tarot_chat.handle_tarot_idle_choice_callback in callbacks


@pytest.mark.asyncio
async def test_idle_tarot_message_prompts_route_choice_without_calling_tarot_provider():
    user_id = 222001
    state.set_tarot_mode(user_id, True)
    state.set_tarot_session(
        user_id,
        {
            "spread_type": "tarot",
            "drawn_cards": [],
            "history": [],
            "waiting_for_question": True,
            "last_activity_at": 1.0,
        },
    )
    update = DummyUpdate("Что ты умеешь?", user_id=user_id)
    context = DummyContext()

    with (
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.tarot_chat.get_tarot_idle_confirm_after_seconds", return_value=60),
        patch("app.handlers.tarot_chat.get_provider_router") as router_factory,
    ):
        await tarot_chat.handle_tarot_message(update, context)

    router_factory.assert_not_called()
    session = state.get_tarot_session(user_id)
    assert session["pending_idle_text"] == "Что ты умеешь?"
    assert session["last_activity_at"] == 1.0
    update.message.reply_text.assert_awaited_once()
    reply_kwargs = update.message.reply_text.await_args.kwargs
    assert isinstance(reply_kwargs["reply_markup"], ReplyKeyboardRemove)
    update.message.reply_text.return_value.edit_text.assert_awaited_once()
    edit_kwargs = update.message.reply_text.return_value.edit_text.await_args.kwargs
    assert isinstance(edit_kwargs["reply_markup"], InlineKeyboardMarkup)
    labels = [button.text for row in edit_kwargs["reply_markup"].inline_keyboard for button in row]
    assert "Продолжить сеанс Таро" in labels
    assert "Отправить как обычный запрос" in labels


@pytest.mark.asyncio
async def test_idle_tarot_llm_choice_clears_tarot_mode_and_processes_pending_text():
    user_id = 222002
    state.set_tarot_mode(user_id, True)
    state.set_tarot_session(
        user_id,
        {
            "spread_type": "tarot",
            "drawn_cards": [],
            "history": [],
            "waiting_for_question": True,
            "last_activity_at": 1000.0,
            "pending_idle_text": "таро",
        },
    )
    update = DummyCallbackUpdate("tarot_idle:llm", user_id=user_id)
    context = DummyContext()

    with (
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as process_long_request,
    ):
        await tarot_chat.handle_tarot_idle_choice_callback(update, context)

    assert not state.is_in_tarot_mode(user_id)
    assert state.get_tarot_session(user_id) is None
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once_with("🤔 Думаю...")
    process_long_request.assert_awaited_once()
    args = process_long_request.await_args.args
    kwargs = process_long_request.await_args.kwargs
    assert args == (update.callback_query.message, update, context)
    assert kwargs["text_override"] == "таро"


@pytest.mark.asyncio
async def test_idle_tarot_continue_choice_processes_pending_text_in_tarot_mode():
    user_id = 222003
    state.set_tarot_mode(user_id, True)
    state.set_tarot_session(
        user_id,
        {
            "spread_type": "tarot",
            "drawn_cards": [],
            "history": [],
            "waiting_for_question": True,
            "last_activity_at": 1000.0,
            "pending_idle_text": "Что показывает расклад?",
        },
    )
    update = DummyCallbackUpdate("tarot_idle:continue", user_id=user_id)
    context = DummyContext()
    router = MagicMock()
    router.get_response = AsyncMock(return_value=("Ответ таролога", "gemini-3.1-flash-lite"))

    with (
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.handlers.tarot_chat.time.time", return_value=2000.0),
        patch("app.handlers.tarot_chat.get_provider_router", return_value=router),
        patch(
            "app.handlers.tarot_chat.draw_cards",
            return_value=[{"name": "Маг", "orientation": "Прямая", "meanings": ["воля"]}] * 3,
        ),
    ):
        await tarot_chat.handle_tarot_idle_choice_callback(update, context)

    assert state.is_in_tarot_mode(user_id)
    session = state.get_tarot_session(user_id)
    assert "pending_idle_text" not in session
    assert session["last_activity_at"] == 2000.0
    assert session["waiting_for_question"] is False
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once_with("🔮 Продолжаю сеанс Таро...")
    router.get_response.assert_awaited_once()

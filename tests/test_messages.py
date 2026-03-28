import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Message, Update
from telegram.ext import ContextTypes

from app.handlers import messages

# --- Mocks for Telegram Objects ---


class MockUser:
    def __init__(self, user_id=123, username="testuser", first_name="Test"):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.is_bot = False


class MockChat:
    def __init__(self, chat_id=456, type="private"):
        self.id = chat_id
        self.type = type


class MockMessage:
    def __init__(self, message_id=789, text="Hello", user=None, chat=None, date=None):
        self.message_id = message_id
        self.text = text
        self.from_user = user
        self.chat = chat
        self.date = date
        self.photo = []
        self.document = None
        self.voice = None
        self.caption = None
        self.media_group_id = None
        self.reply_text = AsyncMock()
        self.edit_text = AsyncMock()


class DummyUpdate:
    def __init__(self, update_id=101, message=None):
        self.update_id = update_id
        self.message = message
        self.effective_user = message.from_user if message else None
        self.effective_chat = message.chat if message else None


class DummyContext:
    def __init__(self):
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.user_data = {}
        self.chat_data = {}


# --- Tests for handle_request ---


@pytest.mark.asyncio
async def test_handle_request_invalid_update():
    """Test handle_request with invalid update object."""
    await messages.handle_request(None, DummyContext())
    # Should log error and return immediately - no exceptions raised

    update = DummyUpdate()
    update.effective_user = None
    await messages.handle_request(update, DummyContext())
    # Should log error and return immediately


@pytest.mark.asyncio
async def test_handle_request_invalid_user_id():
    """Test handle_request with invalid user_id."""
    user = MockUser(user_id=0)  # Invalid ID
    chat = MockChat()
    message = MockMessage(user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    # Mocking bind_request_span since it's used as a context manager
    with patch("app.handlers.messages.bind_request_span") as mock_span:
        mock_span.return_value.__enter__.return_value = None

        # Mock set_request_id
        with patch("app.handlers.messages.set_request_id"):
            await messages.handle_request(update, context)
            # Should log error and return


@pytest.mark.asyncio
async def test_handle_request_rate_limit_exceeded():
    """Test handle_request when rate limit is exceeded."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.api_logger") as _mock_logger,
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = False  # Rate limit exceeded

        await messages.handle_request(update, context)

        mock_rate_limit.assert_awaited_once_with(123)
        message.reply_text.assert_awaited_once()
        assert "Превышен лимит запросов" in message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_request_unauthorized():
    """Test handle_request when user is not authorized."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.api_logger") as _mock_logger,
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = False  # Not authorized

        await messages.handle_request(update, context)

        mock_is_auth.assert_awaited_once_with(123)
        # Should return without doing anything else
        message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_request_text_message_happy_path():
    """Test handle_request with a valid text message (happy path)."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello AI", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    # Capture coroutines passed to submit_task and create_task
    # so we can drive them deterministically instead of letting them
    # run as real background tasks (which would block the event loop
    # waiting on unmocked infrastructure like process_long_request).
    captured_coros = []

    def capture_submit(coro, *args, **kwargs):
        captured_coros.append(coro)
        noop_task = AsyncMock()
        noop_task.cancel = MagicMock()
        return noop_task

    async def _debounce_passthrough(uid, text):
        return text

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.api_logger") as _mock_logger,
        patch("app.handlers.messages.state.get_user_lock") as mock_lock,
        patch("app.handlers.messages.asyncio.create_task") as mock_create_task,
        patch("app.utils.background_tasks.submit_task", side_effect=capture_submit) as mock_submit,
        patch("app.middleware.debounce.debounce_text_message", side_effect=_debounce_passthrough),
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None
        mock_lock.return_value.locked.return_value = False

        await messages.handle_request(update, context)

        # Verify submit_task was called with the task_wrapper coroutine
        assert mock_submit.called

        # Clean up: close captured heartbeat coroutines (from create_task)
        for call_args in mock_create_task.call_args_list:
            coro = call_args[0][0]
            coro.close()

        # Close captured submit_task coroutines (task_wrapper)
        for coro in captured_coros:
            coro.close()


@pytest.mark.asyncio
async def test_handle_request_text_message_happy_path_with_task_execution():
    """Test handle_request ensuring the background task is executed."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello AI", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    # Capture coroutines so we can drive them deterministically.
    captured_coros = []

    def capture_submit(coro, *args, **kwargs):
        captured_coros.append(coro)
        noop_task = AsyncMock()
        noop_task.cancel = MagicMock()
        return noop_task

    async def _debounce_passthrough(uid, text):
        return text

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.state.ensure_state_loaded", new_callable=AsyncMock),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.api_logger") as _mock_logger,
        patch("app.handlers.messages.state.get_user_lock") as mock_lock,
        patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process,
        patch("app.handlers.messages.asyncio.create_task") as mock_create_task,
        patch("app.utils.background_tasks.submit_task", side_effect=capture_submit),
        patch("app.handlers.messages.metrics_collector", AsyncMock()),
        # Prevent xdist cross-test contamination: dedup uses module-level dict
        # shared across tests on the same worker. Without this mock, an earlier
        # test with the same user_id+text poisons the 3s dedup window.
        patch("app.middleware.dedup.is_duplicate_request", new_callable=AsyncMock, return_value=False),
        patch("app.middleware.debounce.debounce_text_message", side_effect=_debounce_passthrough),
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None
        mock_lock.return_value.locked.return_value = False

        await messages.handle_request(update, context)

        # Verify initial placeholder message
        message.reply_text.assert_awaited_with("🤔 Думаю...")

        # Close heartbeat coroutine (from create_task)
        for call_args in mock_create_task.call_args_list:
            coro = call_args[0][0]
            coro.close()

        # Execute the captured task_wrapper coroutine to test the AI pipeline
        for coro in captured_coros:
            await coro

        mock_agent_process.assert_awaited_once()
        # Verify arguments passed to process_long_request
        # args: placeholder_message, update, context
        args, _ = mock_agent_process.await_args
        assert args[1] == update
        assert args[2] == context


@pytest.mark.asyncio
async def test_handle_request_photo_message():
    """Test handle_request with a photo message."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text=None, user=user, chat=chat)
    message.photo = [MagicMock()]  # Assume list of PhotoSize
    update = DummyUpdate(message=message)
    context = DummyContext()

    captured_coros = []

    def capture_submit(coro, *args, **kwargs):
        captured_coros.append(coro)
        noop_task = AsyncMock()
        noop_task.cancel = MagicMock()
        return noop_task

    # Heartbeat coroutines captured via create_task mock (NOT real tasks)
    heartbeat_coros = []

    def mock_create_task(coro, **kwargs):
        heartbeat_coros.append(coro)
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        return mock_task

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.api_logger") as _mock_logger,
        patch("app.handlers.messages.state.get_user_lock") as mock_lock,
        patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process,
        patch("app.handlers.messages.asyncio.create_task", side_effect=mock_create_task),
        patch("app.utils.background_tasks.submit_task", side_effect=capture_submit),
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None
        mock_lock.return_value.locked.return_value = False

        await messages.handle_request(update, context)

        message.reply_text.assert_awaited_with("🖼️ Обрабатываю изображение...")

        for coro in captured_coros:
            await coro

        # Close heartbeat coroutines to avoid warnings
        for coro in heartbeat_coros:
            coro.close()

        mock_agent_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_request_document():
    """Test handle_request with a document."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text=None, user=user, chat=chat)
    message.document = MagicMock()
    message.document.file_name = "test.pdf"
    update = DummyUpdate(message=message)
    context = DummyContext()

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.handle_document", new_callable=AsyncMock) as mock_handle_doc,
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True

        await messages.handle_request(update, context)

        mock_handle_doc.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_handle_request_exception_handling():
    """Test error handling when process_long_request fails."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello Error", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    captured_coros = []

    def capture_submit(coro, *args, **kwargs):
        captured_coros.append(coro)
        noop_task = AsyncMock()
        noop_task.cancel = MagicMock()
        return noop_task

    # Heartbeat coroutines captured via create_task mock (NOT real tasks)
    heartbeat_coros = []

    def mock_create_task(coro, **kwargs):
        heartbeat_coros.append(coro)
        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        return mock_task

    async def _debounce_passthrough(uid, text):
        return text

    with (
        patch("app.handlers.messages.bind_request_span") as mock_span,
        patch("app.handlers.messages.set_request_id"),
        patch("app.handlers.messages.settings") as mock_settings,
        patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit,
        patch("app.handlers.messages.is_authorized", new_callable=AsyncMock) as mock_is_auth,
        patch("app.handlers.messages.api_logger") as _mock_logger,
        patch("app.handlers.messages.state.get_user_lock") as mock_lock,
        patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process,
        patch("app.handlers.messages.asyncio.create_task", side_effect=mock_create_task),
        patch("app.utils.background_tasks.submit_task", side_effect=capture_submit),
        patch("app.middleware.debounce.debounce_text_message", side_effect=_debounce_passthrough),
    ):
        mock_settings.TELEGRAM_MESSAGE_LIMIT = 4096
        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None
        mock_lock.return_value.locked.return_value = False

        # Simulate exception
        mock_agent_process.side_effect = Exception("Agent Error")

        await messages.handle_request(update, context)

        for coro in captured_coros:
            await coro

        # Close heartbeat coroutines to avoid warnings
        for coro in heartbeat_coros:
            coro.close()

        # Verify that placeholder message was edited to show error
        placeholder_mock = message.reply_text.return_value
        call_args = placeholder_mock.edit_text.call_args
        assert "❌ Произошла ошибка при обработке запроса" in call_args[0][0]
        assert "reply_markup" in call_args[1]

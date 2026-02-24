import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, User, Chat, Message
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
    user = MockUser(user_id=0) # Invalid ID
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

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.api_logger") as mock_logger:

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = False # Rate limit exceeded

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

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.api_logger") as mock_logger:

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = False # Not authorized

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

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.api_logger") as mock_logger, \
         patch("app.handlers.messages.state.get_user_lock") as mock_lock, \
         patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process:

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None

        await messages.handle_request(update, context)
        # Note: Background task might not have run yet.

@pytest.mark.asyncio
async def test_handle_request_text_message_happy_path_with_task_execution():
    """Test handle_request ensuring the background task is executed."""
    user = MockUser(user_id=123)
    chat = MockChat(chat_id=456)
    message = MockMessage(text="Hello AI", user=user, chat=chat)
    update = DummyUpdate(message=message)
    context = DummyContext()

    # We need to capture the task created by handle_request
    created_tasks = []
    original_create_task = asyncio.create_task

    def side_effect_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.api_logger") as mock_logger, \
         patch("app.handlers.messages.state.get_user_lock") as mock_lock, \
         patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process, \
         patch("asyncio.create_task", side_effect=side_effect_create_task):

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None

        await messages.handle_request(update, context)

        # Verify initial placeholder message
        message.reply_text.assert_awaited_with("🤔 Думаю...")

        # Wait for the background task to complete
        if created_tasks:
            await asyncio.gather(*created_tasks)

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
    message.photo = [MagicMock()] # Assume list of PhotoSize
    update = DummyUpdate(message=message)
    context = DummyContext()

    created_tasks = []
    original_create_task = asyncio.create_task
    def side_effect_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.api_logger") as mock_logger, \
         patch("app.handlers.messages.state.get_user_lock") as mock_lock, \
         patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process, \
         patch("asyncio.create_task", side_effect=side_effect_create_task):

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None

        await messages.handle_request(update, context)

        message.reply_text.assert_awaited_with("🖼️ Обрабатываю изображение...")

        if created_tasks:
            await asyncio.gather(*created_tasks)

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

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.handle_document", new_callable=AsyncMock) as mock_handle_doc:

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

    created_tasks = []
    original_create_task = asyncio.create_task
    def side_effect_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with patch("app.handlers.messages.bind_request_span") as mock_span, \
         patch("app.handlers.messages.set_request_id"), \
         patch("app.handlers.messages.check_user_rate_limit", new_callable=AsyncMock) as mock_rate_limit, \
         patch("app.handlers.messages.db.is_authorized", new_callable=AsyncMock) as mock_is_auth, \
         patch("app.handlers.messages.api_logger") as mock_logger, \
         patch("app.handlers.messages.state.get_user_lock") as mock_lock, \
         patch("app.handlers.agent.process_long_request", new_callable=AsyncMock) as mock_agent_process, \
         patch("asyncio.create_task", side_effect=side_effect_create_task):

        mock_span.return_value.__enter__.return_value = None
        mock_rate_limit.return_value = True
        mock_is_auth.return_value = True
        mock_lock.return_value.__aenter__.return_value = None
        mock_lock.return_value.__aexit__.return_value = None

        # Simulate exception
        mock_agent_process.side_effect = Exception("Agent Error")

        await messages.handle_request(update, context)

        if created_tasks:
            await asyncio.gather(*created_tasks)

        # Verify that placeholder message was edited to show error
        placeholder_mock = message.reply_text.return_value
        placeholder_mock.edit_text.assert_awaited_with("❌ Произошла ошибка при обработке запроса.")

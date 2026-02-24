import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock
import pytest

# Mock all dependencies (executed only during module execution, not collection)
_mock_keys = [
    "telegram",
    "telegram.ext",
    "app.database",
    "app.config",
    "app.state",
    "app.metrics",
    "app.handlers.menus",
    "app.document_processor",
    "app.prompts",
    "app.utils.formatting",
    "app.utils.decorators",
    "app.utils.time",
    "app.cache",
    "app.queue",
    "app.group_chat",
    "google.genai",
    "app.request_context",
    "app.errors",
]

_original_modules = {}


def setup_module(module):
    global _original_modules
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]

        # Special handling for decorators to be pass-through
        if k == "app.utils.decorators":
            mock_decorators = MagicMock()
            mock_decorators.authorized_only = lambda func: func
            mock_decorators.admin_only = lambda func: func
            sys.modules[k] = mock_decorators
        else:
            sys.modules[k] = MagicMock()


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


async def async_test_new_chat_command():
    # Import inside the test function to ensure mocks are in place
    # We need to reload app.handlers.commands if it was already imported,
    # but since we manipulate sys.modules before import, it should be fine
    # if this test runs in isolation or if setup_module works correctly.
    if "app.handlers.commands" in sys.modules:
        del sys.modules["app.handlers.commands"]

    from app.handlers import commands
    # Re-import mocks to set return values
    from app import database as db
    from app.utils.formatting import TelegramFormatter
    from app.request_context import set_request_id

    # Create mocks
    update = MagicMock()
    context = MagicMock()

    # Mock user and chat
    user_id = 12345
    chat_id = 67890
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.update_id = 999
    update.message.reply_text = AsyncMock()

    # Mock chat state
    chat_state = MagicMock()
    chat_state.history = ["some history"]
    chat_state.token_count = 100
    chat_state.system_prompt = "some prompt"
    chat_state.search_enabled = False
    chat_state.model = "gemini-pro"

    # Mock DB response
    # db is a MagicMock, so we configure it
    db.get_user_chat = AsyncMock(return_value=chat_state)
    db.update_user_chat = AsyncMock()

    # Mock Formatter
    TelegramFormatter.format_text.return_value = ("Formatted text", "Markdown")

    # Mock menus (imported in commands.py)
    # Since we mocked app.handlers.menus in sys.modules, commands.menus refers to that mock
    commands.menus = sys.modules["app.handlers.menus"]

    # Execute
    await commands.new_chat_command(update, context)

    # Assertions

    # 1. Check if DB was queried correctly
    db.get_user_chat.assert_called_with(user_id)

    # 2. Check if state was reset
    assert chat_state.history == []
    assert chat_state.token_count == 0
    assert chat_state.system_prompt is None

    # 3. Check if DB was updated
    db.update_user_chat.assert_called_with(user_id, chat_state)

    # 4. Check if reply was sent
    update.message.reply_text.assert_called()
    call_args = update.message.reply_text.call_args
    assert call_args[0][0] == "Formatted text"
    assert call_args[1]['parse_mode'] == "Markdown"
    assert 'reply_markup' in call_args[1]

    # 5. Check if set_request_id was called
    # This assertion is expected to fail before code modification
    set_request_id.assert_called_with(f"tgcmd-newchat-{chat_id}-999")


def test_new_chat_command():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_test_new_chat_command())
    loop.close()


if __name__ == "__main__":
    test_new_chat_command()

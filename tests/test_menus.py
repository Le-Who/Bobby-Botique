import pytest
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# ==============================================================================
# MOCKS
# ==============================================================================

# Mock external dependencies and app modules to prevent import side effects
mock_db = MagicMock()
mock_db.get_user_chat = AsyncMock()
sys.modules['app.database'] = mock_db

mock_config = MagicMock()
mock_settings = MagicMock()
mock_settings.AVAILABLE_MODELS = ['gemini-pro']
mock_settings.OPENROUTER_AVAILABLE_MODELS = []
mock_settings.DEFAULT_MODEL = 'gemini-pro'
mock_config.settings = mock_settings
sys.modules['app.config'] = mock_config

mock_metrics = MagicMock()
mock_metrics.metrics_collector = MagicMock()
sys.modules['app.metrics'] = mock_metrics

mock_doc_processor = MagicMock()
mock_doc_processor.get_user_documents = AsyncMock(return_value=[])
sys.modules['app.document_processor'] = mock_doc_processor

# Mock time utils and pytz
sys.modules['pytz'] = MagicMock()
sys.modules['app.utils.time'] = MagicMock()

# Mock telegram
mock_telegram = MagicMock()
sys.modules['telegram'] = mock_telegram
sys.modules['telegram.ext'] = MagicMock()

# Mock google.genai and redis
sys.modules['google.genai'] = MagicMock()
sys.modules['redis'] = MagicMock()

# Import the module under test
# We need to ensure app.utils.formatting uses the real text_format or a mock
# Since we want to test content, using the real formatter is better if possible.
# app.utils.formatting imports app.utils.text_format.
# app.utils.text_format imports re, html, logging. All standard.
# So we can let it import naturally.

from app.handlers.menus import get_start_menu_content

# ==============================================================================
# TESTS
# ==============================================================================

def test_start_menu_content_search_on_prompt_set():
    """Test start menu content when search is enabled and system prompt is set."""
    # Setup chat_state mock
    chat_state = MagicMock()
    chat_state.search_enabled = True
    chat_state.system_prompt = "You are a helpful assistant."
    chat_state.model = "gemini-pro"

    # Execute
    text, parse_mode, reply_markup = get_start_menu_content(chat_state)

    # Verify Text
    assert "🟢 ВКЛЮЧЕН" in text
    assert "You are a helpful assistant" in text
    assert "gemini-pro" in text
    assert parse_mode == 'HTML'

    # Verify Keyboard
    # Inspect the inline keyboard structure
    # reply_markup.inline_keyboard is a list of lists of InlineKeyboardButton
    # Since we mocked telegram, InlineKeyboardButton is a Mock.
    # We need to verify how it was called or inspect the objects created.

    # However, since we import from app.handlers.menus, and that module imports InlineKeyboardButton from telegram,
    # and we mocked telegram module, the InlineKeyboardButton used in the code is the Mock class.

    # Let's check the structure of the constructed keyboard
    assert isinstance(reply_markup, MagicMock) # It's a mock because InlineKeyboardMarkup is from telegram

    # Retrieve the keyboard list passed to InlineKeyboardMarkup constructor
    # The code does: InlineKeyboardMarkup(keyboard)
    # We can check the arguments passed to the constructor.

    # Since InlineKeyboardMarkup is a class in the mocked telegram module:
    # mock_telegram.InlineKeyboardMarkup was called with `keyboard` list.

    args, _ = mock_telegram.InlineKeyboardMarkup.call_args
    keyboard = args[0]

    # Keyboard layout:
    # Row 0: New Chat, Models
    # Row 1: Roles, Help
    # Row 2: Search Toggle

    assert len(keyboard) == 3

    # Check Row 0
    row0 = keyboard[0]
    assert len(row0) == 2
    # Since InlineKeyboardButton is mocked, we check the calls or attributes if set
    # The code: InlineKeyboardButton("🆕 Новый чат", callback_data="new_chat")
    # This creates an instance of the mock.

    # We can check the attributes of the mock instances if the mock framework stored them.
    # But usually MagicMock constructor calls don't automatically set attributes unless side_effect does.
    # However, we can check the call history of InlineKeyboardButton to see if it was called with expected args.

    # Better yet, let's look at the logic.
    # The function returns `InlineKeyboardMarkup(keyboard)`.
    # `keyboard` is a list of lists of `InlineKeyboardButton(...)`.
    # These elements are instances of `mock_telegram.InlineKeyboardButton`.

    # Let's verify InlineKeyboardButton was called with expected arguments for the search button.
    # The search button is in the last row.
    search_btn = keyboard[2][0]

    # We can't easily peek inside the mock instance unless we configured it.
    # But we can check if `InlineKeyboardButton` was called with expected text.
    # The text for search button should contain "🟢" because search_enabled is True.

    # Let's gather all calls to InlineKeyboardButton
    calls = mock_telegram.InlineKeyboardButton.call_args_list

    # We expect one of the calls to have text containing "Поиск: 🟢"
    found_search_button = False
    for call in calls:
        args, kwargs = call
        text = args[0] if args else kwargs.get('text')
        callback_data = kwargs.get('callback_data')

        if "Поиск: 🟢" in text and callback_data == "toggle_search":
            found_search_button = True
            break

    assert found_search_button, "Search button with '🟢' not found"

def test_start_menu_content_search_off_prompt_unset():
    """Test start menu content when search is disabled and system prompt is not set."""
    # Reset mocks to clear call history
    mock_telegram.reset_mock()

    # Setup chat_state mock
    chat_state = MagicMock()
    chat_state.search_enabled = False
    chat_state.system_prompt = None # Unset
    chat_state.model = "gpt-4"

    # Execute
    text, parse_mode, reply_markup = get_start_menu_content(chat_state)

    # Verify Text
    assert "🔴 ВЫКЛЮЧЕН" in text
    assert "Не задана" in text
    assert "gpt-4" in text

    # Verify Search Button
    calls = mock_telegram.InlineKeyboardButton.call_args_list
    found_search_button = False
    for call in calls:
        args, kwargs = call
        text_arg = args[0] if args else kwargs.get('text')
        callback_data = kwargs.get('callback_data')

        if "Поиск: 🔴" in text_arg and callback_data == "toggle_search":
            found_search_button = True
            break

    assert found_search_button, "Search button with '🔴' not found"

def test_start_menu_buttons_structure():
    """Verify the overall structure of the start menu buttons."""
    mock_telegram.reset_mock()

    chat_state = MagicMock()
    chat_state.search_enabled = True
    chat_state.system_prompt = "test"
    chat_state.model = "test-model"

    get_start_menu_content(chat_state)

    # Verify InlineKeyboardMarkup was called
    assert mock_telegram.InlineKeyboardMarkup.called

    # Verify specific buttons exist (New Chat, Models, Roles, Help)
    expected_buttons = [
        ("🆕 Новый чат", "new_chat"),
        ("⚙️ Модели", "model_menu"),
        ("🎭 Роли", "open_roles"),
        ("📚 Справка", "help")
    ]

    calls = mock_telegram.InlineKeyboardButton.call_args_list

    for expected_text, expected_callback in expected_buttons:
        found = False
        for call in calls:
            args, kwargs = call
            text = args[0] if args else kwargs.get('text')
            callback_data = kwargs.get('callback_data')
            if text == expected_text and callback_data == expected_callback:
                found = True
                break
        assert found, f"Button '{expected_text}' with callback '{expected_callback}' not found"

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

import pytest
import sys
import importlib
from unittest.mock import MagicMock, patch
from telegram import InlineKeyboardButton

# Ensure we have a clean menus module with real telegram
@pytest.fixture(scope="module")
def menus_module():
    # If telegram is mocked globally (by other tests), remove the mock
    if 'telegram' in sys.modules and isinstance(sys.modules['telegram'], MagicMock):
        del sys.modules['telegram']
        if 'telegram.ext' in sys.modules: del sys.modules['telegram.ext']
        if 'telegram.error' in sys.modules: del sys.modules['telegram.error']

    # Reload or import app.handlers.menus
    if 'app.handlers.menus' in sys.modules:
        return importlib.reload(sys.modules['app.handlers.menus'])
    else:
        import app.handlers.menus
        return app.handlers.menus

@pytest.fixture
def mock_chat_state():
    state = MagicMock()
    state.model = "gemini-flash-latest"
    return state

@pytest.fixture
def mock_context():
    context = MagicMock()
    context.user_data = {}
    return context

@pytest.fixture
def mock_settings(menus_module):
    with patch.object(menus_module, 'settings') as mock:
        yield mock

@pytest.fixture
def mock_get_openrouter_keys(menus_module):
    with patch.object(menus_module, 'get_openrouter_keys') as mock:
        yield mock

def test_get_model_menu_content_gemini_only(menus_module, mock_chat_state, mock_context, mock_settings, mock_get_openrouter_keys):
    # Setup
    mock_settings.AVAILABLE_MODELS = ["gemini-flash-latest", "gemini-pro"]
    mock_settings.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_openrouter_keys.return_value = []

    mock_chat_state.model = "gemini-flash-latest"

    # Execution
    text, parse_mode, reply_markup = menus_module.get_model_menu_content(mock_chat_state, mock_context)

    # Verification
    assert "Google Gemini" in text
    assert "gemini-flash-latest" in text
    assert reply_markup is not None

    # Check buttons
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("gemini-flash-latest" in btn for btn in buttons)
    assert any("gemini-pro" in btn for btn in buttons)

    # Verify no separator
    assert not any("─────────────" in btn for btn in buttons)

def test_get_model_menu_content_openrouter_only(menus_module, mock_chat_state, mock_context, mock_settings, mock_get_openrouter_keys):
    # Setup
    mock_settings.AVAILABLE_MODELS = []
    mock_settings.OPENROUTER_AVAILABLE_MODELS = ["openai/gpt-4", "anthropic/claude-3"]
    mock_get_openrouter_keys.return_value = ["sk-or-key"]

    mock_chat_state.model = "openai/gpt-4"

    # Execution
    text, parse_mode, reply_markup = menus_module.get_model_menu_content(mock_chat_state, mock_context)

    # Verification
    assert "OpenRouter" in text
    assert "openai/gpt-4" in text

    # Check buttons
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("gpt-4" in btn for btn in buttons)
    assert any("claude-3" in btn for btn in buttons)

def test_get_model_menu_content_mixed(menus_module, mock_chat_state, mock_context, mock_settings, mock_get_openrouter_keys):
    # Setup
    mock_settings.AVAILABLE_MODELS = ["gemini-flash"]
    mock_settings.OPENROUTER_AVAILABLE_MODELS = ["openai/gpt-4"]
    mock_get_openrouter_keys.return_value = ["sk-or-key"]

    mock_chat_state.model = "gemini-flash"

    # Execution
    text, parse_mode, reply_markup = menus_module.get_model_menu_content(mock_chat_state, mock_context)

    # Verification
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]

    # Verify both are present
    assert any("gemini-flash" in btn for btn in buttons)
    assert any("gpt-4" in btn for btn in buttons)

    # Verify separator
    assert any("─────────────" in btn for btn in buttons)

def test_get_model_menu_content_no_models(menus_module, mock_chat_state, mock_context, mock_settings, mock_get_openrouter_keys):
    # Setup
    mock_settings.AVAILABLE_MODELS = []
    mock_settings.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_openrouter_keys.return_value = []

    # Execution
    text, parse_mode, reply_markup = menus_module.get_model_menu_content(mock_chat_state, mock_context)

    # Verification
    assert "❌ Нет доступных моделей" in text
    assert reply_markup is None

def test_context_update(menus_module, mock_chat_state, mock_context, mock_settings, mock_get_openrouter_keys):
    # Setup
    mock_settings.AVAILABLE_MODELS = ["gemini-1"]
    mock_settings.OPENROUTER_AVAILABLE_MODELS = ["or-1"]
    mock_get_openrouter_keys.return_value = ["key"]

    # Execution
    menus_module.get_model_menu_content(mock_chat_state, mock_context)

    # Verification
    assert "model_list" in mock_context.user_data
    assert "gemini-1" in mock_context.user_data["model_list"]
    assert "or-1" in mock_context.user_data["model_list"]

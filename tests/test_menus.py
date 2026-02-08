import sys
from unittest.mock import MagicMock

# Mock telegram module
mock_telegram = MagicMock()
class MockInlineKeyboardButton:
    def __init__(self, text, callback_data=None):
        self.text = text
        self.callback_data = callback_data

class MockInlineKeyboardMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard

mock_telegram.InlineKeyboardButton = MockInlineKeyboardButton
mock_telegram.InlineKeyboardMarkup = MockInlineKeyboardMarkup
sys.modules['telegram'] = mock_telegram

# Mock pytz
mock_pytz = MagicMock()
mock_pytz.timezone.return_value = MagicMock()
mock_pytz.UTC = MagicMock()
sys.modules['pytz'] = mock_pytz

# Mock pydantic
mock_pydantic = MagicMock()
class MockBaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

mock_pydantic.BaseModel = MockBaseModel
mock_pydantic.ValidationError = Exception
sys.modules['pydantic'] = mock_pydantic

# Mock app.database
mock_db = MagicMock()
sys.modules['app.database'] = mock_db

# Mock app.document_processor
mock_doc_proc = MagicMock()
sys.modules['app.document_processor'] = mock_doc_proc

# Now import the module under test
from app.handlers import menus
import pytest
from unittest.mock import patch

# Mock chat state object
class ChatState:
    def __init__(self, model):
        self.model = model

@pytest.fixture
def mock_context():
    context = MagicMock()
    context.user_data = {}
    return context

@patch('app.handlers.menus.settings')
@patch('app.handlers.menus.get_openrouter_keys')
def test_get_model_menu_content_gemini_only(mock_get_keys, mock_settings_obj, mock_context):
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-pro", "gemini-flash"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_keys.return_value = [] # No OpenRouter keys

    chat_state = ChatState(model="gemini-pro")

    text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, mock_context)

    assert "gemini-pro" in text
    assert "Google Gemini" in text

    # Check buttons
    # reply_markup.inline_keyboard is a list of lists of InlineKeyboardButton
    buttons = reply_markup.inline_keyboard
    # Expected: gemini-pro (selected), gemini-flash, back

    # Button 1: gemini-pro
    assert "✅ 🤖 gemini-pro" in buttons[0][0].text

    # Button 2: gemini-flash
    assert "🤖 gemini-flash" in buttons[1][0].text
    assert "✅" not in buttons[1][0].text

    # Back button should be last
    assert "⬅️ Назад" in buttons[-1][0].text

@patch('app.handlers.menus.settings')
@patch('app.handlers.menus.get_openrouter_keys')
def test_get_model_menu_content_mixed(mock_get_keys, mock_settings_obj, mock_context):
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-pro"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = ["provider/model-a"]
    mock_get_keys.return_value = ["sk-or-key"]

    chat_state = ChatState(model="provider/model-a")

    text, parse_mode, reply_markup = menus.get_model_menu_content(chat_state, mock_context)

    assert "provider/model-a" in text
    assert "OpenRouter" in text

    buttons = reply_markup.inline_keyboard
    # Expected: gemini-pro, separator, provider/model-a (selected), back

    # Button 1: gemini-pro
    assert "🤖 gemini-pro" in buttons[0][0].text
    assert "✅" not in buttons[0][0].text

    # Separator
    assert "─────────────" in buttons[1][0].text

    # Button 3: provider/model-a
    # Display name logic: m.split("/")[-1] if "/" in m else m
    # "provider/model-a" -> "model-a"
    assert "✅ 🌐 model-a" in buttons[2][0].text

    # Back button
    assert "⬅️ Назад" in buttons[-1][0].text

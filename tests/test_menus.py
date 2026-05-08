# ruff: noqa: E402
import importlib
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Isolate this module in its own xdist worker to prevent sys.modules contamination
# of other test workers. setup_module() installs heavy module-level mocks.
pytestmark = pytest.mark.xdist_group("sys_modules_isolation")

# ==============================================================================
# PYTEST MARKERS - для разделения типов тестов
# ==============================================================================
# Добавьте в pytest.ini:
# [pytest]
# markers =
#     unit: Unit tests with full mocking
#     integration: Integration tests with real dependencies
#     slow: Tests that take longer to execute

# ==============================================================================
# MOCKS FOR UNIT TESTS
# ==============================================================================


def setup_mocks():
    """Setup all mocks for unit tests."""
    # Mock external dependencies
    mock_db = MagicMock()
    mock_db.get_user_chat = AsyncMock()
    sys.modules["app.database"] = mock_db

    mock_config = MagicMock()
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-pro", "gemini-flash"]
    mock_settings.OPENCODE_AVAILABLE_MODELS = []
    mock_settings.OPENROUTER_AVAILABLE_MODELS = []
    mock_settings.DEFAULT_MODEL = "gemini-pro"
    mock_config.settings = mock_settings
    sys.modules["app.config"] = mock_config

    mock_metrics = MagicMock()
    mock_metrics.metrics_collector = MagicMock()
    sys.modules["app.metrics"] = mock_metrics

    mock_doc_processor = MagicMock()
    mock_doc_processor.get_user_documents = AsyncMock(return_value=[])
    sys.modules["app.document_processor"] = mock_doc_processor

    # Mock time utils
    sys.modules["app.utils.time"] = MagicMock()

    # Mock telegram
    mock_telegram = MagicMock()

    class MockInlineKeyboardButton:
        """Mock for Telegram InlineKeyboardButton."""

        def __init__(self, text: str, callback_data: str | None = None, **kwargs):
            self.text = text
            self.callback_data = callback_data
            for k, v in kwargs.items():
                setattr(self, k, v)

        def __repr__(self):
            return f"Button(text={self.text!r}, callback={self.callback_data!r})"

    class MockInlineKeyboardMarkup:
        """Mock for Telegram InlineKeyboardMarkup."""

        def __init__(self, inline_keyboard: list[list[MockInlineKeyboardButton]]):
            self.inline_keyboard = inline_keyboard

        def __repr__(self):
            return f"Keyboard(rows={len(self.inline_keyboard)})"

    mock_telegram.InlineKeyboardButton = MockInlineKeyboardButton
    mock_telegram.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    sys.modules["telegram"] = mock_telegram
    sys.modules["telegram.constants"] = MagicMock()
    sys.modules["telegram.ext"] = MagicMock()

    # Mock pydantic
    mock_pydantic = MagicMock()

    class MockBaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    mock_pydantic.BaseModel = MockBaseModel
    mock_pydantic.ValidationError = Exception
    sys.modules["pydantic"] = mock_pydantic

    # Mock google.genai and redis (mock app.cache to avoid corrupting the real
    # redis package entry in sys.modules — setup_module runs before conftest
    # autouse fixtures which import crocodile_runtime → app.cache → redis.asyncio)
    sys.modules["google.genai"] = MagicMock()
    sys.modules["google.genai.errors"] = MagicMock()
    mock_cache = MagicMock()
    mock_cache.redis_client = MagicMock()
    sys.modules["app.cache"] = mock_cache

    # Mock other needed parts, but do not override app.handlers package
    return MockInlineKeyboardButton, MockInlineKeyboardMarkup


# Setup mocks at module level for unit tests
_mocked_module_keys = [
    "app.database",
    "app.config",
    "app.metrics",
    "app.document_processor",
    "app.utils.time",
    "telegram",
    "telegram.constants",
    "telegram.ext",
    "pydantic",
    "google.genai",
    "google.genai.errors",
    "app.cache",
]


# Save original modules before mocking
def setup_module(module):
    global MockInlineKeyboardButton, MockInlineKeyboardMarkup, _original_modules
    import importlib

    _original_modules = {}
    _original_modules["__app_keys_before__"] = {k for k in sys.modules if k.startswith("app.")}
    for k in _mocked_module_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]

    MockInlineKeyboardButton, MockInlineKeyboardMarkup = setup_mocks()

    if "app.handlers.menus" in sys.modules:
        importlib.reload(sys.modules["app.handlers.menus"])


def teardown_module(module):
    """Restore sys.modules to prevent test pollution."""
    app_keys_before = _original_modules.pop("__app_keys_before__", set())
    for k in _mocked_module_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)

    # Purge ALL app.* modules imported during the mocked period.
    # They hold stale MagicMock bindings that persist even after sys.modules restore.
    for k in list(sys.modules):
        if k.startswith("app.") and k not in app_keys_before:
            del sys.modules[k]


# ==============================================================================
# FIXTURES
# ==============================================================================


class ChatState:
    """Mock ChatState for testing."""

    def __init__(
        self,
        model: str,
        search_enabled: bool = False,
        system_prompt: str | None = None,
    ):
        self.model = model
        self.search_enabled = search_enabled
        self.system_prompt = system_prompt


@pytest.fixture
def mock_context():
    """Fixture providing mock context."""
    context = MagicMock()
    context.user_data = {}
    return context


@pytest.fixture
def chat_state():
    """Fixture providing basic chat state."""
    return ChatState(model="gemini-pro")


# Integration test fixture
@pytest.fixture(scope="module")
def menus_module():
    """
    Fixture for integration tests with real Telegram.
    Ensures clean module import with real dependencies.
    """
    # Remove telegram mocks if present
    if "telegram" in sys.modules and isinstance(sys.modules["telegram"], MagicMock):
        del sys.modules["telegram"]
        for module in ["telegram.ext", "telegram.error"]:
            if module in sys.modules:
                del sys.modules[module]

    # Reload menus module
    if "app.handlers.menus" in sys.modules:
        return importlib.reload(sys.modules["app.handlers.menus"])
    else:
        import app.handlers.menus

        return app.handlers.menus


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================


def verify_button(
    keyboard: list[list[Any]],
    row: int,
    col: int,
    expected_text: str,
    expected_callback: str,
    partial_match: bool = False,
) -> None:
    """
    Verify a button at specific position has expected text and callback.
    Works with both mocked and real Telegram buttons.
    """
    assert 0 <= row < len(keyboard), f"Row index {row} out of bounds. Keyboard has {len(keyboard)} rows"
    assert 0 <= col < len(keyboard[row]), (
        f"Column index {col} out of bounds. Row {row} has {len(keyboard[row])} columns"
    )

    button = keyboard[row][col]

    if partial_match:
        assert expected_text in button.text, f"Expected '{expected_text}' in button text, got '{button.text}'"
    else:
        assert button.text == expected_text, f"Expected button text '{expected_text}', got '{button.text}'"

    assert button.callback_data == expected_callback, (
        f"Expected callback '{expected_callback}', got '{button.callback_data}'"
    )


def verify_response_structure(
    response: tuple[str, str, Any],
    expected_parse_mode: str = "HTML",
    allow_none_markup: bool = False,
) -> None:
    """Verify response has correct structure and parse mode."""
    assert len(response) == 3, f"Expected 3-tuple, got {len(response)} elements"
    text, parse_mode, reply_markup = response
    assert isinstance(text, str), f"Expected text to be str, got {type(text)}"
    assert parse_mode == expected_parse_mode, f"Expected parse_mode '{expected_parse_mode}', got '{parse_mode}'"

    if not allow_none_markup:
        assert reply_markup is not None, "Expected reply_markup, got None"


def find_button_by_text(keyboard: list[list[Any]], text_substring: str) -> tuple[int, int]:
    """Find button position by text substring."""
    for row_idx, row in enumerate(keyboard):
        for col_idx, button in enumerate(row):
            if text_substring in button.text:
                return row_idx, col_idx
    raise AssertionError(f"Button with text containing '{text_substring}' not found in keyboard")


def extract_button_texts(keyboard: list[list[Any]]) -> list[str]:
    """Extract all button texts from keyboard for easier assertions."""
    return [btn.text for row in keyboard for btn in row]


# ==============================================================================
# UNIT TESTS - Using mocks (fast, isolated)
# ==============================================================================


# Import after mocks are set up
def get_menu_methods():
    from app.handlers.menus import get_model_menu_content, get_start_menu_content

    return get_start_menu_content, get_model_menu_content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_menu_content_search_on_prompt_set():
    """Test start menu content when search is enabled and system prompt is set."""
    chat_state = ChatState(
        model="gemini-pro",
        search_enabled=True,
        system_prompt="You are a helpful assistant.",
    )

    get_start_menu_content, _ = get_menu_methods()
    response = await get_start_menu_content(chat_state)
    verify_response_structure(response, "HTML")
    text, parse_mode, reply_markup = response

    # Verify text content
    assert "🟢" in text, "Search status indicator missing"
    assert "You are a helpful assistant" in text, "System prompt not displayed"
    assert "gemini-pro" in text, "Model name not displayed"

    # Verify keyboard structure
    keyboard = reply_markup.inline_keyboard
    assert len(keyboard) >= 4, f"Expected at least 4 rows, got {len(keyboard)}"

    # Verify search button dynamically
    search_row, search_col = find_button_by_text(keyboard, "Поиск: 🟢")
    verify_button(
        keyboard,
        search_row,
        search_col,
        "Поиск: 🟢",
        "toggle_search",
        partial_match=True,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_menu_content_search_off_prompt_unset():
    """Test start menu content when search is disabled and system prompt is not set."""
    chat_state = ChatState(model="gemini-pro", search_enabled=False, system_prompt=None)

    get_start_menu_content, _ = get_menu_methods()
    response = await get_start_menu_content(chat_state)
    verify_response_structure(response, "HTML")
    text, _, reply_markup = response

    # Verify text content
    assert "🔴" in text, "Search disabled status missing"
    assert "gemini-pro" in text, "Model name not displayed"

    # Verify search button
    keyboard = reply_markup.inline_keyboard
    search_row, search_col = find_button_by_text(keyboard, "Поиск: 🔴")
    verify_button(
        keyboard,
        search_row,
        search_col,
        "Поиск: 🔴",
        "toggle_search",
        partial_match=True,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_menu_buttons_structure():
    """Verify the overall structure of the start menu buttons."""
    chat_state = ChatState(model="test-model", search_enabled=True, system_prompt="test")

    get_start_menu_content, _ = get_menu_methods()
    response = await get_start_menu_content(chat_state)
    verify_response_structure(response, "HTML")
    _, _, reply_markup = response

    keyboard = reply_markup.inline_keyboard

    # Find and verify buttons dynamically
    new_chat_pos = find_button_by_text(keyboard, "Новый чат")
    verify_button(keyboard, *new_chat_pos, "💬 Новый чат", "new_chat", partial_match=True)

    models_pos = find_button_by_text(keyboard, "Модель")
    verify_button(keyboard, *models_pos, "🧠 Модель AI", "model_menu", partial_match=True)

    roles_pos = find_button_by_text(keyboard, "Роли")
    verify_button(keyboard, *roles_pos, "🎭 Роли", "open_roles", partial_match=True)

    help_pos = find_button_by_text(keyboard, "Помощь")
    verify_button(keyboard, *help_pos, "❓ Помощь", "help", partial_match=True)

    # Verify new document/conversation buttons
    docs_pos = find_button_by_text(keyboard, "Документы")
    verify_button(keyboard, *docs_pos, "📄 Документы", "open_documents", partial_match=True)

    conv_pos = find_button_by_text(keyboard, "Беседы")
    verify_button(keyboard, *conv_pos, "💬 Беседы", "open_conversations", partial_match=True)


@pytest.mark.unit
@patch("app.handlers.menus.get_all_available_models")
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_gemini_only(mock_get_keys, mock_settings_obj, mock_get_all, mock_context):
    """Test model menu with only Gemini models available."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-pro", "gemini-flash"]
    mock_settings_obj.OPENCODE_AVAILABLE_MODELS = []
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_settings_obj.FREETHEAI_AVAILABLE_MODELS = []
    mock_get_keys.return_value = []
    mock_get_all.return_value = ["gemini-pro", "gemini-flash"]

    chat_state = ChatState(model="gemini-pro")

    _, get_model_menu_content = get_menu_methods()
    response = get_model_menu_content(chat_state, mock_context)
    verify_response_structure(response)
    text, _, reply_markup = response

    # Verify text content
    assert "gemini-pro" in text, "Selected model not in text"
    assert "Google Gemini" in text, "Provider name missing"

    keyboard = reply_markup.inline_keyboard
    button_texts = extract_button_texts(keyboard)

    # Verify both models present
    assert any("gemini-pro" in btn for btn in button_texts)
    assert any("gemini-flash" in btn for btn in button_texts)

    # Verify no separator (only one provider)
    assert not any("─────────────" in btn for btn in button_texts)

    # Verify selected model has checkmark
    gemini_pro_pos = find_button_by_text(keyboard, "gemini-pro")
    assert "✅" in keyboard[gemini_pro_pos[0]][gemini_pro_pos[1]].text


@pytest.mark.unit
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_openrouter_only(mock_get_keys, mock_settings_obj, mock_context):
    """Test model menu with only OpenRouter models available."""
    mock_settings_obj.AVAILABLE_MODELS = []
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = [
        "openai/gpt-4",
        "anthropic/claude-3",
    ]
    mock_get_keys.return_value = ["sk-or-key"]

    chat_state = ChatState(model="openai/gpt-4")

    _, get_model_menu_content = get_menu_methods()
    response = get_model_menu_content(chat_state, mock_context)
    verify_response_structure(response)
    text, _, reply_markup = response

    # Verify text content
    assert "OpenRouter" in text, "OpenRouter provider name missing"
    assert "openai/gpt-4" in text or "gpt-4" in text

    keyboard = reply_markup.inline_keyboard
    button_texts = extract_button_texts(keyboard)

    # Verify models present (short names)
    assert any("gpt-4" in btn for btn in button_texts)
    assert any("claude-3" in btn for btn in button_texts)


@pytest.mark.unit
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_mixed(mock_get_keys, mock_settings_obj, mock_context):
    """Test model menu with both Gemini and OpenRouter models."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-flash"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = ["openai/gpt-4"]
    mock_get_keys.return_value = ["sk-or-key"]

    chat_state = ChatState(model="gemini-flash")

    _, get_model_menu_content = get_menu_methods()
    response = get_model_menu_content(chat_state, mock_context)
    verify_response_structure(response)
    text, _, reply_markup = response

    keyboard = reply_markup.inline_keyboard
    button_texts = extract_button_texts(keyboard)

    # Verify both providers present
    assert any("gemini-flash" in btn for btn in button_texts)
    assert any("gpt-4" in btn for btn in button_texts)

    # Verify separator exists (multiple providers)
    assert any("─────────────" in btn for btn in button_texts)


@pytest.mark.unit
@patch("app.handlers.menus.get_all_available_models")
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_no_models(mock_get_keys, mock_settings_obj, mock_get_all, mock_context):
    """Test model menu when no models are available."""
    mock_settings_obj.AVAILABLE_MODELS = []
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_keys.return_value = []
    mock_get_all.return_value = []

    chat_state = ChatState(model="nonexistent-model")

    _, get_model_menu_content = get_menu_methods()
    response = get_model_menu_content(chat_state, mock_context)
    verify_response_structure(response, expected_parse_mode=None, allow_none_markup=True)
    text, _, reply_markup = response

    # Should show error message
    assert "❌ Нет доступных моделей" in text or "Нет доступных" in text

    # Markup can be None when no models available, or contain just a back button
    if reply_markup is not None:
        assert len(reply_markup.inline_keyboard) >= 1, "If markup present, must have at least one row"


@pytest.mark.unit
@patch("app.handlers.menus.get_all_available_models")
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_context_update(mock_get_keys, mock_settings_obj, mock_get_all, mock_context):
    """Test that context.user_data is updated with model list."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-1"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = ["or-1"]
    mock_get_keys.return_value = ["key"]
    mock_get_all.return_value = ["gemini-1", "or-1"]

    chat_state = ChatState(model="gemini-1")

    _, get_model_menu_content = get_menu_methods()
    get_model_menu_content(chat_state, mock_context)

    # Verify context was updated
    assert "model_list" in mock_context.user_data
    assert "gemini-1" in mock_context.user_data["model_list"]
    assert "or-1" in mock_context.user_data["model_list"]


# ==============================================================================
# EDGE CASE TESTS
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "model,search_enabled,prompt",
    [
        ("", False, None),  # Empty model
        ("model-with-very-long-name-that-exceeds-normal-length", True, "prompt"),
        ("gemini-pro", False, "A" * 500),  # Very long prompt
        ("special/model:v1.0", True, "Prompt with 特殊字符"),  # Special characters
    ],
)
@pytest.mark.asyncio
async def test_start_menu_edge_cases(model, search_enabled, prompt):
    """Test start menu with edge case inputs."""
    chat_state = ChatState(model=model, search_enabled=search_enabled, system_prompt=prompt)

    get_start_menu_content, _ = get_menu_methods()
    response = await get_start_menu_content(chat_state)
    verify_response_structure(response)
    text, _, reply_markup = response

    # Should not crash and return valid structure
    assert isinstance(text, str)
    assert len(reply_markup.inline_keyboard) > 0


@pytest.mark.unit
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_model_menu_nonexistent_selected_model(mock_get_keys, mock_settings_obj, mock_context):
    """Test model menu when selected model is not in available models."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-pro"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_keys.return_value = []

    chat_state = ChatState(model="nonexistent-model")

    _, get_model_menu_content = get_menu_methods()
    response = get_model_menu_content(chat_state, mock_context)
    verify_response_structure(response)

    text, _, reply_markup = response

    # Should not crash and handle gracefully
    keyboard = reply_markup.inline_keyboard
    assert len(keyboard) > 0


# ==============================================================================
# HELPER FUNCTION TESTS
# ==============================================================================


@pytest.mark.unit
def test_verify_button_out_of_bounds():
    """Test that verify_button raises AssertionError for out of bounds indices."""
    keyboard = [[MockInlineKeyboardButton("Test", "callback")]]

    with pytest.raises(AssertionError, match="Row index .* out of bounds"):
        verify_button(keyboard, 5, 0, "Test", "callback")

    with pytest.raises(AssertionError, match="Column index .* out of bounds"):
        verify_button(keyboard, 0, 5, "Test", "callback")


@pytest.mark.unit
def test_find_button_by_text_not_found():
    """Test that find_button_by_text raises AssertionError when button not found."""
    keyboard = [[MockInlineKeyboardButton("Test", "callback")]]

    with pytest.raises(AssertionError, match="Button with text containing .* not found"):
        find_button_by_text(keyboard, "Nonexistent")


@pytest.mark.unit
def test_extract_button_texts():
    """Test extract_button_texts helper function."""
    keyboard = [
        [
            MockInlineKeyboardButton("Button1", "cb1"),
            MockInlineKeyboardButton("Button2", "cb2"),
        ],
        [MockInlineKeyboardButton("Button3", "cb3")],
    ]

    texts = extract_button_texts(keyboard)
    assert texts == ["Button1", "Button2", "Button3"]


# ==============================================================================
# INTEGRATION TESTS - Using real Telegram (slower, comprehensive)
# ==============================================================================


@pytest.mark.integration
@pytest.mark.slow
def test_integration_real_telegram_buttons():
    """
    Integration test with real Telegram objects.
    Temporarily restores original modules, reimports menus, and tests
    with the real telegram library to ensure our mock-based unit tests
    aren't masking compatibility issues.
    """
    import importlib

    # Temporarily restore original modules to get a clean import
    saved = {}
    for k in _mocked_module_keys:
        if k in sys.modules:
            saved[k] = sys.modules.pop(k)

    # Restore originals that were backed up
    sys.modules.update(_original_modules)

    try:
        # Fresh import with real libraries
        menus = importlib.import_module("app.handlers.menus")
        importlib.reload(menus)

        from telegram import InlineKeyboardButton

        context = MagicMock()
        context.user_data = {}

        with (
            patch.object(menus, "settings") as mock_settings_obj,
            patch.object(menus, "get_openrouter_keys", return_value=[]),
        ):
            mock_settings_obj.AVAILABLE_MODELS = ["gemini-flash-latest", "gemini-pro"]
            mock_settings_obj.OPENCODE_AVAILABLE_MODELS = []
            mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []

            cs = ChatState(model="gemini-flash-latest")
            text, parse_mode, reply_markup = menus.get_model_menu_content(cs, context)

        # Verify structure with real Telegram objects
        assert "Google Gemini" in text
        assert "gemini-flash-latest" in text
        assert reply_markup is not None

        # Real InlineKeyboardButton objects
        buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
        assert any("gemini-flash-latest" in btn for btn in buttons)
        assert any("gemini-pro" in btn for btn in buttons)
        assert all(isinstance(btn, InlineKeyboardButton) for row in reply_markup.inline_keyboard for btn in row)
    finally:
        # Restore the mocked state for any remaining tests in this module
        for k in _mocked_module_keys:
            if k in _original_modules:
                sys.modules.pop(k, None)
        sys.modules.update(saved)
        if "app.handlers.menus" in sys.modules:
            importlib.reload(sys.modules["app.handlers.menus"])


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    # Run with: pytest test_menus.py -v -m unit  # Only unit tests
    # Run with: pytest test_menus.py -v -m integration  # Only integration tests
    # Run with: pytest test_menus.py -v  # All tests
    sys.exit(pytest.main(["-v", __file__]))

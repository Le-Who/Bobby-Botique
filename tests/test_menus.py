import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from telegram import InlineKeyboardButton

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
# UNIT TESTS - Real menu rendering with scoped configuration
# ==============================================================================


# Use the real module with scoped configuration patches
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
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_gemini_only(mock_get_keys, mock_settings_obj, mock_context):
    """Test model menu with only Gemini models available."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-pro", "gemini-flash"]
    mock_settings_obj.OPENCODE_AVAILABLE_MODELS = []
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_settings_obj.FREETHEAI_AVAILABLE_MODELS = []
    mock_get_keys.return_value = []

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
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_get_model_menu_content_no_models(mock_get_keys, mock_settings_obj, mock_context):
    """Test model menu when no models are available."""
    mock_settings_obj.AVAILABLE_MODELS = []
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = []
    mock_get_keys.return_value = []

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
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_context_update(mock_get_keys, mock_settings_obj, mock_context):
    """Test that context.user_data is updated with model list."""
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-1"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = ["or-1"]
    mock_get_keys.return_value = ["key"]

    chat_state = ChatState(model="gemini-1")

    _, get_model_menu_content = get_menu_methods()
    get_model_menu_content(chat_state, mock_context)

    # Verify context was updated
    assert "model_list" in mock_context.user_data
    assert "gemini-1" in mock_context.user_data["model_list"]
    assert "or-1" in mock_context.user_data["model_list"]


@pytest.mark.unit
@patch("app.handlers.menus.settings")
@patch("app.handlers.menus.get_openrouter_keys")
def test_context_model_order_excludes_openrouter_without_keys(mock_get_keys, mock_settings_obj, mock_context):
    mock_settings_obj.AVAILABLE_MODELS = ["gemini-1"]
    mock_settings_obj.OPENCODE_AVAILABLE_MODELS = ["opencode-go/glm-5"]
    mock_settings_obj.OPENROUTER_AVAILABLE_MODELS = ["vendor/hidden"]
    mock_settings_obj.FREETHEAI_AVAILABLE_MODELS = ["cat/visible"]
    mock_get_keys.return_value = []
    _, get_model_menu_content = get_menu_methods()
    get_model_menu_content(ChatState(model="gemini-1"), mock_context)

    assert mock_context.user_data["model_list"] == [
        "gemini-1",
        "opencode-go/glm-5",
        "cat/visible",
    ]


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
    keyboard = [[InlineKeyboardButton("Test", callback_data="callback")]]

    with pytest.raises(AssertionError, match="Row index .* out of bounds"):
        verify_button(keyboard, 5, 0, "Test", "callback")

    with pytest.raises(AssertionError, match="Column index .* out of bounds"):
        verify_button(keyboard, 0, 5, "Test", "callback")


@pytest.mark.unit
def test_find_button_by_text_not_found():
    """Test that find_button_by_text raises AssertionError when button not found."""
    keyboard = [[InlineKeyboardButton("Test", callback_data="callback")]]

    with pytest.raises(AssertionError, match="Button with text containing .* not found"):
        find_button_by_text(keyboard, "Nonexistent")


@pytest.mark.unit
def test_extract_button_texts():
    """Test extract_button_texts helper function."""
    keyboard = [
        [
            InlineKeyboardButton("Button1", callback_data="cb1"),
            InlineKeyboardButton("Button2", callback_data="cb2"),
        ],
        [InlineKeyboardButton("Button3", callback_data="cb3")],
    ]

    texts = extract_button_texts(keyboard)
    assert texts == ["Button1", "Button2", "Button3"]


# ==============================================================================
# TELEGRAM OBJECT CONTRACT
# ==============================================================================


@pytest.mark.unit
def test_model_menu_uses_real_telegram_buttons(mock_context):
    from app.handlers import menus

    with (
        patch.object(menus, "settings") as settings,
        patch.object(menus, "get_openrouter_keys", return_value=[]),
    ):
        settings.AVAILABLE_MODELS = ["gemini-flash-latest", "gemini-pro"]
        settings.OPENCODE_AVAILABLE_MODELS = []
        settings.OPENROUTER_AVAILABLE_MODELS = []
        settings.FREETHEAI_AVAILABLE_MODELS = []
        text, _, markup = menus.get_model_menu_content(ChatState(model="gemini-flash-latest"), mock_context)

    assert "Google Gemini" in text
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert all(isinstance(button, InlineKeyboardButton) for button in buttons)
    assert any("gemini-flash-latest" in button.text for button in buttons)
    assert any("gemini-pro" in button.text for button in buttons)


# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    # Run with: pytest test_menus.py -v -m unit  # Only unit tests
    # Run with: pytest test_menus.py -v  # All tests
    sys.exit(pytest.main(["-v", __file__]))

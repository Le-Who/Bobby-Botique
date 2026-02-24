# ruff: noqa: E402
import pytest
import sys
import importlib
from unittest.mock import MagicMock, AsyncMock, patch
from typing import List, Tuple, Any, Optional

# ==============================================================================
# MOCKS FOR UNIT TESTS
# ==============================================================================

def setup_mocks():
    """Setup all mocks for unit tests."""
    # Mock external dependencies
    mock_db = MagicMock()
    mock_db.get_user_chat = AsyncMock()
    mock_db.db_query = AsyncMock()
    mock_db.get_role_data = AsyncMock()
    sys.modules["app.database"] = mock_db

    mock_config = MagicMock()
    mock_settings = MagicMock()
    mock_settings.AVAILABLE_MODELS = ["gemini-pro", "gemini-flash"]
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

    # Mock time utils and pytz
    mock_pytz = MagicMock()
    mock_pytz.timezone.return_value = MagicMock()
    mock_pytz.UTC = MagicMock()
    sys.modules["pytz"] = mock_pytz
    sys.modules["app.utils.time"] = MagicMock()

    # Mock telegram
    mock_telegram = MagicMock()

    class MockInlineKeyboardButton:
        """Mock for Telegram InlineKeyboardButton."""
        def __init__(self, text: str, callback_data: Optional[str] = None):
            self.text = text
            self.callback_data = callback_data

        def __repr__(self):
            return f"Button(text={self.text!r}, callback={self.callback_data!r})"

    class MockInlineKeyboardMarkup:
        """Mock for Telegram InlineKeyboardMarkup."""
        def __init__(self, inline_keyboard: List[List[MockInlineKeyboardButton]]):
            self.inline_keyboard = inline_keyboard

        def __repr__(self):
            return f"Keyboard(rows={len(self.inline_keyboard)})"

    mock_telegram.InlineKeyboardButton = MockInlineKeyboardButton
    mock_telegram.InlineKeyboardMarkup = MockInlineKeyboardMarkup
    sys.modules["telegram"] = mock_telegram
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

    # Mock google.genai and redis
    sys.modules["google.genai"] = MagicMock()
    sys.modules["redis"] = MagicMock()

    return MockInlineKeyboardButton, MockInlineKeyboardMarkup


# Setup mocks at module level for unit tests
_mocked_module_keys = [
    "app.database",
    "app.config",
    "app.metrics",
    "app.document_processor",
    "pytz",
    "app.utils.time",
    "telegram",
    "telegram.ext",
    "pydantic",
    "google.genai",
    "redis",
]

_original_modules = {}

def setup_module(module):
    global MockInlineKeyboardButton, MockInlineKeyboardMarkup, _original_modules

    _original_modules = {}
    for k in _mocked_module_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]

    MockInlineKeyboardButton, MockInlineKeyboardMarkup = setup_mocks()

    if "app.handlers.menus" in sys.modules:
        importlib.reload(sys.modules["app.handlers.menus"])


def teardown_module(module):
    """Restore sys.modules to prevent test pollution."""
    for k in _mocked_module_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)

    # Remove the poisoned module so subsequent tests import cleanly
    if "app.handlers.menus" in sys.modules:
        del sys.modules["app.handlers.menus"]


# ==============================================================================
# FIXTURES
# ==============================================================================

class ChatState:
    """Mock ChatState for testing."""
    def __init__(
        self,
        model: str,
        search_enabled: bool = False,
        system_prompt: Optional[str] = None,
    ):
        self.model = model
        self.search_enabled = search_enabled
        self.system_prompt = system_prompt


@pytest.fixture
def chat_state():
    """Fixture providing basic chat state."""
    return ChatState(model="gemini-pro")

@pytest.fixture
def mock_db():
    mock = sys.modules["app.database"]
    mock.db_query.reset_mock(side_effect=True, return_value=True)
    mock.get_role_data.reset_mock(side_effect=True, return_value=True)
    # Default return values
    mock.db_query.return_value = []
    mock.get_role_data.return_value = None
    return mock

# ==============================================================================
# TEST CASES
# ==============================================================================

@pytest.mark.asyncio
async def test_hub_view_no_active_role(chat_state, mock_db):
    """Test hub view when no role is active."""
    from app.handlers.menus import get_roles_menu_content

    chat_state.system_prompt = None
    mock_db.db_query.return_value = [{"count": 5}]  # 5 custom roles

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="hub"
    )

    assert "Базовая (без роли)" in text
    assert "📂 Мои роли (5)" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "📚 Каталог ролей" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "➕ Создать новую роль" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    # Should not have "Отключить роль"
    assert "🛑 Отключить роль" not in [btn.text for row in reply_markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_hub_view_active_system_role(chat_state, mock_db):
    """Test hub view when a system role is active."""
    from app.handlers.menus import get_roles_menu_content
    from app import prompts

    # Assume "teacher" role exists in DEFAULT_ROLES
    role_key = "teacher"
    role_prompt = prompts.DEFAULT_ROLES[role_key]["prompt"]
    role_title = prompts.DEFAULT_ROLES[role_key]["title"]

    chat_state.system_prompt = role_prompt
    mock_db.db_query.return_value = [{"count": 0}]

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="hub"
    )

    assert role_title in text
    assert "🛑 Отключить роль" in [btn.text for row in reply_markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_hub_view_active_custom_role(chat_state, mock_db):
    """Test hub view when a custom role is active."""
    from app.handlers.menus import get_roles_menu_content

    custom_role_prompt = "You are a custom role."
    custom_role_title = "My Custom Role"

    chat_state.system_prompt = custom_role_prompt

    # Mock db_query calls.
    # 1. Search for custom role (in get_roles_menu_content)
    # 2. Count custom roles (in _get_roles_hub_content)
    mock_db.db_query.side_effect = [
        [{"id": 10, "title": custom_role_title, "prompt": custom_role_prompt}],  # role search
        [{"count": 1}],  # count
    ]

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="hub"
    )

    assert custom_role_title in text
    assert "🛑 Отключить роль" in [btn.text for row in reply_markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_list_view_my_roles_empty(chat_state, mock_db):
    """Test my_roles view when list is empty."""
    from app.handlers.menus import get_roles_menu_content

    mock_db.db_query.return_value = []  # No roles

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="my_roles"
    )

    assert "У вас пока нет сохраненных ролей" in text
    assert "➕ Создать" in [btn.text for row in reply_markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_list_view_my_roles_items(chat_state, mock_db):
    """Test my_roles view with items and pagination."""
    from app.handlers.menus import get_roles_menu_content

    roles = [{"id": i, "title": f"Role {i}"} for i in range(1, 15)]
    mock_db.db_query.return_value = roles

    # Page 0
    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="my_roles",
        page=0
    )

    assert "Role 1" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "Role 6" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "Role 7" not in [btn.text for row in reply_markup.inline_keyboard for btn in row]

    # Pagination buttons
    assert "➡️" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "1/3" in [btn.text for row in reply_markup.inline_keyboard for btn in row]

    # Page 1
    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="my_roles",
        page=1
    )

    assert "Role 7" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "⬅️" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert "2/3" in [btn.text for row in reply_markup.inline_keyboard for btn in row]


@pytest.mark.asyncio
async def test_list_view_system_roles(chat_state, mock_db):
    """Test system_roles view."""
    from app.handlers.menus import get_roles_menu_content
    from app import prompts

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="system_roles"
    )

    assert "Каталог встроенных ролей" in text

    # Check for some default roles
    default_role_titles = [meta["title"] for meta in prompts.DEFAULT_ROLES.values()]
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]

    # Ensure at least one default role is present
    assert any(title in btn for title in default_role_titles for btn in buttons)


@pytest.mark.asyncio
async def test_details_view_system_role(chat_state, mock_db):
    """Test details view for a system role."""
    from app.handlers.menus import get_roles_menu_content

    role_key = "teacher"
    role_data = {
        "id": "teacher",
        "title": "Преподаватель",
        "prompt": "You are a teacher...",
        "is_custom": False
    }

    mock_db.get_role_data.return_value = role_data

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="role_details",
        role_key=role_key
    )

    assert "Преподаватель" in text
    assert "✅ Применить роль" in [btn.text for row in reply_markup.inline_keyboard for btn in row]
    # Should not have delete/rename buttons
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert not any("Удалить" in btn for btn in buttons)
    assert not any("Переим." in btn for btn in buttons)


@pytest.mark.asyncio
async def test_details_view_custom_role(chat_state, mock_db):
    """Test details view for a custom role."""
    from app.handlers.menus import get_roles_menu_content

    role_key = "user_role:10"
    role_data = {
        "id": 10,
        "title": "My Custom Role",
        "prompt": "Custom prompt",
        "is_custom": True
    }

    mock_db.get_role_data.return_value = role_data

    text, parse_mode, reply_markup = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="role_details",
        role_key=role_key
    )

    assert "My Custom Role" in text
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Удалить" in btn for btn in buttons)
    assert any("Переим." in btn for btn in buttons)


@pytest.mark.asyncio
async def test_error_conditions(chat_state, mock_db):
    """Test error conditions (invalid view mode, role not found)."""
    from app.handlers.menus import get_roles_menu_content

    # Invalid view mode
    text, _, _ = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="invalid_mode"
    )
    assert "Ошибка режима" in text

    # Role not found
    mock_db.get_role_data.return_value = None
    text, _, _ = await get_roles_menu_content(
        user_id=123,
        chat_state=chat_state,
        view_mode="role_details",
        role_key="nonexistent"
    )
    assert "Роль не найдена" in text

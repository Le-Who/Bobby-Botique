# ruff: noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
def chat_state():
    """Fixture providing basic chat state."""
    return ChatState(model="gemini-pro")


@pytest.fixture
def mock_db():
    mock = MagicMock()
    mock.get_user_chat = AsyncMock(return_value=[])
    mock.db_query = AsyncMock(return_value=[])
    mock.get_role_data = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def run_patched():
    """Context manager fixture to patch all external dependencies of menus.py"""

    async def _run(state, db_mock, view_mode="hub", page=0, role_key=None,
                   custom_roles=None, custom_roles_full=None, custom_role_count=0):
        # We patch inside the app.handlers.menus module where it's used
        with (
            patch("app.handlers.menus.get_role_data", db_mock.get_role_data),
            patch("app.handlers.menus.get_conversation_count", AsyncMock(return_value=0)),
            patch("app.handlers.menus.get_user_conversations", AsyncMock(return_value=[])),
            patch("app.handlers.menus.get_user_custom_roles",
                  AsyncMock(return_value=custom_roles or [])),
            patch("app.handlers.menus.get_user_custom_roles_full",
                  AsyncMock(return_value=custom_roles_full or [])),
            patch("app.handlers.menus.get_custom_role_count",
                  AsyncMock(return_value=custom_role_count)),
            patch("app.handlers.menus.get_user_today_request_count",
                  AsyncMock(return_value=0)),
            patch("app.handlers.menus.settings") as mock_settings,
            patch("app.handlers.menus.TelegramFormatter") as mock_formatter,
        ):
            mock_settings.AVAILABLE_MODELS = ["gemini-pro"]
            mock_settings.DEFAULT_MODEL = "gemini-pro"
            mock_formatter.format_text.side_effect = lambda text: (text, None)

            from app.handlers.menus import get_roles_menu_content

            # The signature is (user_id, chat_state, view_mode="hub", page=0, role_key=None)
            return await get_roles_menu_content(
                user_id=123,
                chat_state=state,
                view_mode=view_mode,
                page=page,
                role_key=role_key,
            )

    return _run


# ==============================================================================
# TEST CASES
# ==============================================================================


@pytest.mark.asyncio
async def test_hub_view_no_active_role(chat_state, mock_db, run_patched):
    """Test hub view when no role is active."""
    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="hub"
    )

    assert "Роли" in text
    assert "Базовая" in text  # It usually defaults to Базовая (без роли)

    # Check buttons text
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Мои роли" in b for b in buttons)
    assert any("Каталог ролей" in b for b in buttons)
    assert any("Сгенерировать" in b for b in buttons)


@pytest.mark.asyncio
async def test_hub_view_active_system_role(chat_state, mock_db, run_patched):
    """Test hub view with an active system role."""
    chat_state.system_prompt = "You are a helpful assistant."

    # Mock system role definition
    with patch(
        "app.handlers.menus.prompts.DEFAULT_ROLES",
        {
            "sys1": {
                "title": "Helpful Assistant",
                "prompt": "You are a helpful assistant.",
            }
        },
    ):
        text, parse_mode, reply_markup = await run_patched(
            chat_state, mock_db, view_mode="hub"
        )

        assert "Активная:" in text
        assert "Helpful Assistant" in text


@pytest.mark.asyncio
async def test_hub_view_active_custom_role(chat_state, mock_db, run_patched):
    """Test hub view with an active custom role."""
    chat_state.system_prompt = "Custom prompt from user."

    # Not a predefined system role, but exists in user's custom roles
    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="hub",
        custom_roles_full=[
            {"id": 10, "title": "My Custom Role", "prompt": "Custom prompt from user."}
        ],
        custom_role_count=1,
    )

    assert "Активная:" in text
    assert "My Custom Role" in text


@pytest.mark.asyncio
async def test_list_view_my_roles_empty(chat_state, mock_db, run_patched):
    """Test 'my_roles' view when user has no roles."""
    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="my_roles",
        custom_roles=[],
    )

    assert "У вас пока нет" in text

    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Сгенерировать" in b for b in buttons)
    assert any("Назад" in b for b in buttons)


@pytest.mark.asyncio
async def test_list_view_my_roles_items(chat_state, mock_db, run_patched):
    """Test 'my_roles' view with user roles."""
    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="my_roles",
        custom_roles=[
            {"id": 1, "title": "Role 1", "prompt": "Desc 1"},
            {"id": 2, "title": "Role 2", "prompt": "Desc 2"},
        ],
    )

    assert "Ваши личные роли" in text

    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Role 1" in b for b in buttons)
    assert any("Role 2" in b for b in buttons)
    assert any("Сгенерировать" in b for b in buttons)


@pytest.mark.asyncio
async def test_list_view_system_roles(chat_state, mock_db, run_patched):
    """Test 'system_roles' view."""
    with patch(
        "app.handlers.menus.prompts.DEFAULT_ROLES",
        {
            "sys1": {"title": "Sys Role 1", "prompt": "p1"},
            "sys2": {"title": "Sys Role 2", "prompt": "p2"},
        },
    ):
        text, parse_mode, reply_markup = await run_patched(
            chat_state, mock_db, view_mode="system_roles"
        )

        assert "Каталог встроенных ролей" in text

        buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
        assert any("Sys Role 1" in b for b in buttons)
        assert any("Sys Role 2" in b for b in buttons)


@pytest.mark.asyncio
async def test_details_view_system_role(chat_state, mock_db, run_patched):
    """Test 'details' view for a system role."""
    # We mock get_role_data
    mock_db.get_role_data.return_value = {
        "id": "sys1",
        "title": "Sys Role 1",
        "prompt": "First sys role prompt",
        "is_custom": False,
    }

    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="role_details", role_key="sys1"
    )

    assert "Sys Role 1" in text
    assert "First sys role prompt" in text

    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Активировать" in b for b in buttons)


@pytest.mark.asyncio
async def test_details_view_custom_role(chat_state, mock_db, run_patched):
    """Test 'details' view for a custom role."""
    mock_db.get_role_data.return_value = {
        "id": 10,
        "title": "My Custom Role",
        "prompt": "My prompt",
        "is_custom": True,
    }

    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="role_details", role_key="user_role:10"
    )

    assert "My Custom Role" in text
    assert "My prompt" in text

    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    assert any("Активировать" in b for b in buttons)
    assert any("Удалить" in b for b in buttons)


@pytest.mark.asyncio
async def test_error_conditions(chat_state, mock_db, run_patched):
    """Test various error conditions."""
    # 1. details block for unknown role
    mock_db.get_role_data.return_value = None

    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="role_details", role_key="unknown"
    )
    assert "Роль не найдена" in text

    # 2. Unknown page
    text, parse_mode, reply_markup = await run_patched(
        chat_state, mock_db, view_mode="invalid_page"
    )
    assert "Ошибка режима" in text

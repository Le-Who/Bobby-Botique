import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from tests.test_roles_menu import ChatState, get_roles_menu_content


async def run_patched(state, db_mock, view_mode="hub", page=0, role_key=None):
    with (
        patch("app.handlers.menus.db", db_mock),
        patch("app.handlers.menus.settings") as mock_settings,
        patch("app.handlers.menus.TelegramFormatter") as mock_formatter,
    ):
        mock_settings.AVAILABLE_MODELS = ["gemini-pro"]
        mock_settings.DEFAULT_MODEL = "gemini-pro"
        mock_formatter.format_text.side_effect = lambda text: (text, None)

        return await get_roles_menu_content(
            user_id=123,
            chat_state=state,
            view_mode=view_mode,
            page=page,
            role_key=role_key,
        )


async def main():
    import dotenv

    dotenv.load_dotenv(override=True)

    state = ChatState(model="gemini-pro")

    mock_db = MagicMock()
    mock_db.get_user_chat = AsyncMock(return_value=[])
    mock_db.db_query = AsyncMock(return_value=[])
    mock_db.get_role_data = AsyncMock(return_value=None)

    # Simulate test_list_view_my_roles_items
    mock_db.db_query.return_value = [
        {"id": 1, "title": "Role 1", "prompt": "Desc 1"},
        {"id": 2, "title": "Role 2", "prompt": "Desc 2"},
    ]

    text, parse_mode, reply_markup = await run_patched(
        state, mock_db, view_mode="my_roles"
    )

    print("--- TEXT ---")
    print(text)

    print("--- BUTTONS ---")
    buttons = [btn.text for row in reply_markup.inline_keyboard for btn in row]
    print(f"Buttons list: {buttons}")
    print(f"Contains 'Role 1': {any('Role 1' in b for b in buttons)}")

    # Simulate test_details_view_system_role
    mock_db.get_role_data.return_value = {
        "id": "sys1",
        "title": "Sys Role 1",
        "prompt": "First sys role prompt",
        "is_custom": False,
    }

    text2, pm2, rm2 = await run_patched(
        state, mock_db, view_mode="role_details", role_key="sys1"
    )
    print("--- TEXT 2 ---")
    print(text2)


if __name__ == "__main__":
    asyncio.run(main())

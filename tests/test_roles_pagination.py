import sys
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import types
import os

@pytest.fixture
def mock_dependencies():
    with patch.dict(sys.modules):
        # Mock dependencies
        mock_db = MagicMock()
        mock_db.db_query = AsyncMock(return_value=[
            {'id': i, 'title': f'Role {i}', 'created_at': '2023-01-01'} for i in range(20)
        ])
        mock_db.get_role_data = AsyncMock()
        sys.modules['app.database'] = mock_db

        sys.modules['app.config'] = MagicMock()
        sys.modules['app.metrics'] = MagicMock()
        sys.modules['app.prompts'] = MagicMock()

        mock_formatting = MagicMock()
        mock_formatting.TelegramFormatter = MagicMock()
        mock_formatting.TelegramFormatter.format_text.return_value = ("text", "HTML")
        sys.modules['app.utils.formatting'] = mock_formatting

        sys.modules['telegram'] = MagicMock()

        # Import the function
        try:
            from app.handlers.menus import _get_roles_list_content
        except ImportError:
            sys.path.append(os.getcwd())
            from app.handlers.menus import _get_roles_list_content

        yield mock_db, _get_roles_list_content

@pytest.mark.asyncio
async def test_pagination_sql_limit(mock_dependencies):
    mock_db, _get_roles_list_content = mock_dependencies

    async def db_query_side_effect(query, params=None):
        if "COUNT(*)" in query:
            return [{'count': 20}]
        else:
            return [{'id': i, 'title': f'Role {i}'} for i in range(6)]

    mock_db.db_query.side_effect = db_query_side_effect

    # Call with page 0
    await _get_roles_list_content(user_id=123, view_mode="my_roles", page=0, active_role_key=None)

    calls = mock_db.db_query.call_args_list

    # Expect 2 calls: count and select with limit
    assert len(calls) >= 2, "Should call count and then select with LIMIT"

    select_call = calls[-1]
    query_str = select_call[0][0]

    assert "LIMIT" in query_str, "Query should use LIMIT"
    assert "OFFSET" in query_str, "Query should use OFFSET"

    params = select_call[0][1]
    # LIMIT 6, OFFSET 0
    assert params[1] == 6 # ITEMS_PER_PAGE
    assert params[2] == 0 # OFFSET

@pytest.mark.asyncio
async def test_pagination_sql_offset(mock_dependencies):
    mock_db, _get_roles_list_content = mock_dependencies

    async def db_query_side_effect(query, params=None):
        if "COUNT(*)" in query:
            return [{'count': 20}]
        else:
            return [{'id': i, 'title': f'Role {i}'} for i in range(6)]

    mock_db.db_query.side_effect = db_query_side_effect

    # Call with page 1
    await _get_roles_list_content(user_id=123, view_mode="my_roles", page=1, active_role_key=None)

    calls = mock_db.db_query.call_args_list

    assert len(calls) >= 2, "Should call count and then select with LIMIT"

    select_call = calls[-1]
    params = select_call[0][1]

    # LIMIT 6, OFFSET 6
    assert params[1] == 6
    assert params[2] == 6

"""Tests for app.repos.roles — custom user roles CRUD operations."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_db_query():
    """Patch db.db_query for roles module."""
    import app.repos.roles

    with patch.object(app.repos.roles.db, "db_query", new_callable=AsyncMock) as m_query:
        yield m_query


@pytest.mark.asyncio
async def test_get_user_custom_roles(mock_db_query):
    from app.repos.roles import get_user_custom_roles

    mock_db_query.return_value = [
        {"id": 1, "title": "Role 1"},
        {"id": 2, "title": "Role 2"},
    ]
    user_id = 42

    result = await get_user_custom_roles(user_id)

    assert result == [{"id": 1, "title": "Role 1"}, {"id": 2, "title": "Role 2"}]
    mock_db_query.assert_called_once_with(
        "SELECT id, title FROM user_roles WHERE user_id = $1 ORDER BY created_at DESC",
        (user_id,),
    )


@pytest.mark.asyncio
async def test_get_user_custom_roles_empty(mock_db_query):
    from app.repos.roles import get_user_custom_roles

    mock_db_query.return_value = []
    user_id = 42

    result = await get_user_custom_roles(user_id)

    assert result == []
    mock_db_query.assert_called_once()


@pytest.mark.asyncio
async def test_get_user_custom_roles_full(mock_db_query):
    from app.repos.roles import get_user_custom_roles_full

    mock_db_query.return_value = [{"id": 1, "title": "Role 1", "prompt": "Prompt 1"}]
    user_id = 42

    result = await get_user_custom_roles_full(user_id)

    assert result == [{"id": 1, "title": "Role 1", "prompt": "Prompt 1"}]
    mock_db_query.assert_called_once_with(
        "SELECT id, title, prompt FROM user_roles WHERE user_id = $1",
        (user_id,),
    )


@pytest.mark.asyncio
async def test_get_custom_role_count(mock_db_query):
    from app.repos.roles import get_custom_role_count

    mock_db_query.return_value = [{"count": 5}]
    user_id = 42

    result = await get_custom_role_count(user_id)

    assert result == 5
    mock_db_query.assert_called_once_with(
        "SELECT COUNT(*) as count FROM user_roles WHERE user_id = $1",
        (user_id,),
    )


@pytest.mark.asyncio
async def test_get_custom_role_count_empty(mock_db_query):
    from app.repos.roles import get_custom_role_count

    mock_db_query.return_value = []
    user_id = 42

    result = await get_custom_role_count(user_id)

    assert result == 0
    mock_db_query.assert_called_once()


@pytest.mark.asyncio
async def test_get_custom_role_prompt(mock_db_query):
    from app.repos.roles import get_custom_role_prompt

    mock_db_query.return_value = [{"prompt": "You are a helpful assistant."}]
    role_id = 1
    user_id = 42

    result = await get_custom_role_prompt(role_id, user_id)

    assert result == "You are a helpful assistant."
    mock_db_query.assert_called_once_with(
        "SELECT prompt FROM user_roles WHERE id = $1 AND user_id = $2",
        (role_id, user_id),
    )


@pytest.mark.asyncio
async def test_get_custom_role_prompt_not_found(mock_db_query):
    from app.repos.roles import get_custom_role_prompt

    mock_db_query.return_value = []
    role_id = 1
    user_id = 42

    result = await get_custom_role_prompt(role_id, user_id)

    assert result is None
    mock_db_query.assert_called_once()


@pytest.mark.asyncio
async def test_create_custom_role(mock_db_query):
    from app.repos.roles import create_custom_role

    user_id = 42
    title = "My Role"
    prompt = "My Prompt"

    await create_custom_role(user_id, title, prompt)

    mock_db_query.assert_called_once_with(
        "INSERT INTO user_roles (user_id, title, prompt) VALUES ($1, $2, $3)",
        (user_id, title, prompt),
    )


@pytest.mark.asyncio
async def test_delete_custom_role(mock_db_query):
    from app.repos.roles import delete_custom_role

    role_id = 1
    user_id = 42

    await delete_custom_role(role_id, user_id)

    mock_db_query.assert_called_once_with(
        "DELETE FROM user_roles WHERE id = $1 AND user_id = $2",
        (role_id, user_id),
    )


@pytest.mark.asyncio
async def test_rename_custom_role(mock_db_query):
    from app.repos.roles import rename_custom_role

    role_id = 1
    user_id = 42
    new_title = "New Title"

    await rename_custom_role(role_id, user_id, new_title)

    mock_db_query.assert_called_once_with(
        "UPDATE user_roles SET title = $1 WHERE id = $2 AND user_id = $3",
        (new_title, role_id, user_id),
    )

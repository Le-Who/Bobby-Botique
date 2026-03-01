import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import database


@pytest.mark.asyncio
async def test_create_rls_policies_users():
    mock_query = AsyncMock()
    # Mocking existence check: return empty list (not existing)
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        await database.create_rls_policies("users")

        # Verify calls
        # Expected: 1. Select check, 2. Create policy
        calls = mock_query.call_args_list
        assert len(calls) >= 2

        # Check if the create policy SQL was executed
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        assert len(creation_sql) > 0
        sql = creation_sql[0]

        assert "CREATE POLICY users_policy ON users" in sql
        assert "current_setting('app.user_id', true)" in sql

@pytest.mark.asyncio
async def test_create_rls_policies_chats():
    mock_query = AsyncMock()
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        await database.create_rls_policies("chats")

        calls = mock_query.call_args_list
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        assert len(creation_sql) > 0
        sql = creation_sql[0]
        assert "CREATE POLICY chats_policy ON chats" in sql

@pytest.mark.asyncio
async def test_create_rls_policies_roles():
    mock_query = AsyncMock()
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        await database.create_rls_policies("roles")

        calls = mock_query.call_args_list
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        # Roles creates multiple policies: read, insert, update, delete
        assert len(creation_sql) >= 2

        read_policy = next((s for s in creation_sql if "roles_read_policy" in s), None)
        assert read_policy is not None

        write_policies = next((s for s in creation_sql if "roles_insert_policy" in s), None)
        assert write_policies is not None

@pytest.mark.asyncio
async def test_create_rls_policies_conversation_messages():
    mock_query = AsyncMock()
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        await database.create_rls_policies("conversation_messages")

        calls = mock_query.call_args_list
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        assert len(creation_sql) > 0
        sql = creation_sql[0]
        assert "CREATE POLICY conversation_messages_policy ON conversation_messages" in sql
        assert "owner_user_id" in sql  # Uses owner_user_id for direct user filtering

@pytest.mark.asyncio
async def test_create_rls_policies_group_chats():
    mock_query = AsyncMock()
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        await database.create_rls_policies("group_chats")

        calls = mock_query.call_args_list
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        assert len(creation_sql) > 0
        sql = creation_sql[0]
        assert "CREATE POLICY group_chats_policy ON group_chats" in sql
        assert "group_members" in sql

@pytest.mark.asyncio
async def test_create_rls_policies_admin_tables():
    mock_query = AsyncMock()
    mock_query.return_value = []

    with patch("app.database.db_query", mock_query):
        # Test one of the admin tables
        await database.create_rls_policies("api_keys")

        calls = mock_query.call_args_list
        creation_sql = [c[0][0] for c in calls if "CREATE POLICY" in c[0][0]]
        assert len(creation_sql) > 0
        sql = creation_sql[0]
        assert "CREATE POLICY api_keys_policy ON api_keys" in sql
        assert "current_setting('app.is_admin', true)" in sql

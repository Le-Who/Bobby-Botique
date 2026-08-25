import inspect
import os
import sys
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import database
from app.db import rls


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

        assert 'CREATE POLICY "users_policy" ON "users"' in sql
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
        assert 'CREATE POLICY "chats_policy" ON "chats"' in sql


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
        assert 'CREATE POLICY "conversation_messages_policy" ON "conversation_messages"' in sql
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
        assert 'CREATE POLICY "group_chats_policy" ON "group_chats"' in sql
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
        assert 'CREATE POLICY "api_keys_policy" ON "api_keys"' in sql
        assert "current_setting('app.is_admin', true)" in sql


@pytest.mark.asyncio
async def test_user_context_is_transaction_local_for_pgbouncer_pooling():
    """RLS GUCs must never leak across tenants in PgBouncer transaction mode."""
    mock_query = AsyncMock()
    connection = object()

    with patch("app.database.db_query", mock_query):
        await database.set_user_context(42, False, conn=connection)
        await database.clear_user_context(conn=connection)

    set_sql = mock_query.await_args_list[0].args[0]
    clear_sql = mock_query.await_args_list[1].args[0]
    assert "set_config('app.user_id', $1, true)" in set_sql
    assert "set_config('app.user_id', '', true)" in clear_sql
    assert all(call.kwargs["conn"] is connection for call in mock_query.await_args_list)


@pytest.mark.parametrize(
    "repository_function",
    [
        pytest.param("app.repos.chats.get_user_chat", id="get-chat"),
        pytest.param("app.repos.chats.update_user_chat", id="update-chat"),
        pytest.param("app.repos.users.is_authorized", id="authorization"),
        pytest.param("app.repos.keys.get_available_gemini_key", id="gemini-key"),
        pytest.param("app.repos.keys.get_available_openrouter_key", id="openrouter-key"),
        pytest.param("app.repos.metrics_repo.get_active_key_info", id="active-key-info"),
        pytest.param("app.documents.repository.get_user_documents", id="documents-list"),
        pytest.param("app.documents.repository.get_document_content", id="document-content"),
        pytest.param("app.web.api_key_health", id="key-health-endpoint"),
    ],
)
def test_rls_repository_scope_keeps_local_context_inside_transaction(repository_function):
    """A transaction-local GUC is useless if the protected query starts later."""
    module_name, function_name = repository_function.rsplit(".", 1)
    module = __import__(module_name, fromlist=[function_name])
    source = inspect.getsource(inspect.unwrap(getattr(module, function_name)))

    assert "pool.acquire() as conn, conn.transaction()" in source


@pytest.mark.asyncio
async def test_create_rls_policies_propagates_policy_creation_failure():
    """A missing tenant policy must be a startup error, not a log-only warning."""
    query = AsyncMock(
        side_effect=[
            [],
            asyncpg.InsufficientPrivilegeError("policy creation denied"),
        ]
    )

    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="policy creation denied"):
        await rls.create_rls_policies("users", query)


@pytest.mark.asyncio
async def test_setup_row_level_security_propagates_alter_failure():
    """Failure to enable RLS on any configured table must fail closed."""

    async def failing_query(sql, params=()):
        del params
        if "ENABLE ROW LEVEL SECURITY" in sql:
            raise asyncpg.InsufficientPrivilegeError("cannot enable RLS")
        return []

    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="cannot enable RLS"):
        await rls.setup_row_level_security(failing_query)

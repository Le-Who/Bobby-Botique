from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from app.database import ChatState
from app.repos import conversations as conv_repo


@pytest.fixture(autouse=True)
def save_pool_boundary(monkeypatch):
    conn, manager = _pool_boundary()
    monkeypatch.setattr(conv_repo, "db_manager", manager)
    return conn


def _pool_boundary():
    conn = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return conn, SimpleNamespace(pool=pool, is_connected=True)


@pytest.fixture
def mock_chat_state():
    return ChatState(
        history=[{"role": "user", "content": "Hello"}],
        model="gpt-4",
        token_count=100,
        search_enabled=False,
        system_prompt=None,
        is_deep_dive=False,
        deep_dive_thread_id=None,
        _original_length=0,
    )


@pytest.mark.asyncio
async def test_save_conversation_success(mock_chat_state):
    user_id = 123
    title = "Test Conversation"
    expected_conv_id = 1

    with (
        patch("app.repos.chats.get_user_chat", new_callable=AsyncMock) as mock_get_user_chat,
        patch("app.repos.conversations.db_query", new_callable=AsyncMock) as mock_db_query,
    ):
        mock_get_user_chat.return_value = mock_chat_state

        mock_db_query.side_effect = [[{"id": expected_conv_id}], []]

        result = await conv_repo.save_conversation(user_id, title)

        assert result == expected_conv_id

        mock_get_user_chat.assert_called_once_with(user_id)

        assert mock_db_query.call_count == 2

        insert_call = mock_db_query.call_args_list[0]
        assert "INSERT INTO" in insert_call[0][0] and "conversations" in insert_call[0][0]
        assert insert_call[0][1] == (
            user_id,
            title,
            None,
            None,
            None,
            mock_chat_state.token_count,
        )

        proc_call = mock_db_query.call_args_list[1]
        assert "CALL save_chat_to_conversation" in proc_call[0][0]
        assert proc_call[0][1] == (user_id, expected_conv_id)


@pytest.mark.asyncio
async def test_save_conversation_no_chat_state():
    user_id = 123
    title = "Test Conversation"

    with patch("app.repos.chats.get_user_chat", new_callable=AsyncMock) as mock_get_user_chat:
        mock_get_user_chat.return_value = None

        result = await conv_repo.save_conversation(user_id, title)

        assert result is None
        mock_get_user_chat.assert_called_once_with(user_id)


@pytest.mark.asyncio
async def test_save_conversation_insert_failure(mock_chat_state):
    user_id = 123
    title = "Test Conversation"

    with (
        patch("app.repos.chats.get_user_chat", new_callable=AsyncMock) as mock_get_user_chat,
        patch("app.repos.conversations.db_query", new_callable=AsyncMock) as mock_db_query,
    ):
        mock_get_user_chat.return_value = mock_chat_state
        mock_db_query.return_value = []

        result = await conv_repo.save_conversation(user_id, title)

        assert result is None


@pytest.mark.asyncio
async def test_save_conversation_no_history():
    user_id = 123
    title = "Test Conversation"
    expected_conv_id = 2

    mock_chat_state_empty = ChatState(
        history=[],
        model="gpt-4",
        token_count=0,
        search_enabled=False,
        system_prompt=None,
        is_deep_dive=False,
        deep_dive_thread_id=None,
        _original_length=0,
    )

    with (
        patch("app.repos.chats.get_user_chat", new_callable=AsyncMock) as mock_get_user_chat,
        patch("app.repos.conversations.db_query", new_callable=AsyncMock) as mock_db_query,
    ):
        mock_get_user_chat.return_value = mock_chat_state_empty
        mock_db_query.return_value = [{"id": expected_conv_id}]

        result = await conv_repo.save_conversation(user_id, title)

        assert result == expected_conv_id

        assert mock_db_query.call_count == 1
        assert "INSERT INTO" in mock_db_query.call_args[0][0] and "conversations" in mock_db_query.call_args[0][0]


@pytest.mark.asyncio
async def test_save_conversation_procedure_failure(mock_chat_state):
    user_id = 123
    title = "Test Conversation"
    expected_conv_id = 3

    with (
        patch("app.repos.chats.get_user_chat", new_callable=AsyncMock) as mock_get_user_chat,
        patch("app.repos.conversations.db_query", new_callable=AsyncMock) as mock_db_query,
        patch("app.repos.conversations.logging.error") as mock_logging_error,
    ):
        mock_get_user_chat.return_value = mock_chat_state

        mock_db_query.side_effect = [
            [{"id": expected_conv_id}],
            asyncpg.PostgresError("Procedure failed"),
        ]

        result = await conv_repo.save_conversation(user_id, title)

        assert result is None

        assert mock_db_query.call_count == 2

        mock_logging_error.assert_called_once()
        assert "Error in save_conversation" in mock_logging_error.call_args[0][0]


@pytest.mark.asyncio
async def test_export_user_conversations_is_complete_and_tenant_scoped():
    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    rows = [
        {
            "id": 71,
            "title": "Saved",
            "messages": '[{"role":"user","content":"hello"}]',
        }
    ]
    with (
        patch.object(
            conv_repo,
            "db_manager",
            SimpleNamespace(pool=pool, is_connected=True),
        ),
        patch.object(conv_repo, "set_user_context", new_callable=AsyncMock) as set_context,
        patch.object(conv_repo, "clear_user_context", new_callable=AsyncMock),
        patch.object(conv_repo, "db_query", new_callable=AsyncMock, return_value=rows) as query,
    ):
        result = await conv_repo.export_user_conversations(42)

    assert result[0]["messages"] == [{"role": "user", "content": "hello"}]
    set_context.assert_awaited_once_with(42, False, conn=conn)
    sql = query.await_args.args[0]
    assert "conversation.user_id = $1" in sql
    assert "message.owner_user_id = conversation.user_id" in sql
    assert query.await_args.kwargs["conn"] is conn

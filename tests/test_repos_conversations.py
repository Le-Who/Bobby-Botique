"""Tests for app.repos.conversations — CRUD, switch, rename, delete."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_deps():
    """Patch db deps for conversations module."""
    from app.repos import conversations as conv

    mock_lock = AsyncMock()
    mock_lock.__aenter__.return_value = None
    mock_lock.__aexit__.return_value = None

    with patch.object(conv, "db_query", new_callable=AsyncMock) as m_query, \
         patch.object(conv, "db_execute_many", new_callable=AsyncMock) as m_exec, \
         patch.object(conv, "db_manager") as m_mgr, \
         patch.object(conv, "reconnect_database", new_callable=AsyncMock):
        m_mgr._cache_lock = mock_lock
        m_mgr._active_chats_cache = {}
        m_mgr.is_connected = True

        mock_conn = MagicMock()
        mock_acq = MagicMock()
        mock_acq.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acq.__aexit__ = AsyncMock(return_value=False)
        m_mgr.pool.acquire.return_value = mock_acq

        yield {"query": m_query, "exec": m_exec, "mgr": m_mgr, "conn": mock_conn}


# ---------------------------------------------------------------------------
# save_conversation
# ---------------------------------------------------------------------------

class TestSaveConversation:
    @pytest.mark.asyncio
    async def test_inserts_and_returns_id(self, mock_deps):
        from app.repos.conversations import save_conversation
        mock_deps["query"].return_value = [{"id": 42}]

        mock_chat = MagicMock()
        mock_chat.history = []
        mock_chat.token_count = 100

        with patch("app.repos.chats.get_user_chat",
                    new_callable=AsyncMock, return_value=mock_chat):
            result = await save_conversation(1, "My Chat", "system", None)
        assert result == 42
        query = mock_deps["query"].call_args[0][0]
        assert "INSERT INTO conversations" in query

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_result(self, mock_deps):
        from app.repos.conversations import save_conversation
        mock_deps["query"].return_value = []

        mock_chat = MagicMock()
        mock_chat.history = []
        mock_chat.token_count = 0

        with patch("app.repos.chats.get_user_chat",
                    new_callable=AsyncMock, return_value=mock_chat):
            result = await save_conversation(1, "My Chat")
        assert result is None


# ---------------------------------------------------------------------------
# get_user_conversations
# ---------------------------------------------------------------------------

class TestGetUserConversations:
    @pytest.mark.asyncio
    async def test_returns_list(self, mock_deps):
        from app.repos.conversations import get_user_conversations
        mock_deps["query"].return_value = [
            {"id": 1, "title": "Chat 1", "role_type": "system", "role_id": None,
             "summary": None, "token_budget": None, "created_at": "2024-01-01",
             "role_title": "Assistant", "user_role_title": None},
            {"id": 2, "title": "Chat 2", "role_type": "custom", "role_id": 5,
             "summary": "A chat", "token_budget": 1000, "created_at": "2024-01-02",
             "role_title": None, "user_role_title": "My Role"},
        ]
        result = await get_user_conversations(42)
        assert len(result) == 2
        query = mock_deps["query"].call_args[0][0]
        assert "SELECT" in query
        assert "conversations" in query


# ---------------------------------------------------------------------------
# get_conversation_count
# ---------------------------------------------------------------------------

class TestGetConversationCount:
    @pytest.mark.asyncio
    async def test_returns_count(self, mock_deps):
        from app.repos.conversations import get_conversation_count
        mock_deps["query"].return_value = [{"count": 5}]
        assert await get_conversation_count(42) == 5

    @pytest.mark.asyncio
    async def test_returns_zero_on_empty(self, mock_deps):
        from app.repos.conversations import get_conversation_count
        mock_deps["query"].return_value = []
        assert await get_conversation_count(42) == 0


# ---------------------------------------------------------------------------
# delete_conversation
# ---------------------------------------------------------------------------

class TestDeleteConversation:
    @pytest.mark.asyncio
    async def test_deletes_and_returns_true(self, mock_deps):
        from app.repos.conversations import delete_conversation
        mock_deps["query"].return_value = [{"id": 10}]
        result = await delete_conversation(42, 10)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_if_not_found(self, mock_deps):
        from app.repos.conversations import delete_conversation
        mock_deps["query"].return_value = []
        result = await delete_conversation(42, 999)
        assert result is False


# ---------------------------------------------------------------------------
# rename_conversation
# ---------------------------------------------------------------------------

class TestRenameConversation:
    @pytest.mark.asyncio
    async def test_updates_title(self, mock_deps):
        from app.repos.conversations import rename_conversation
        mock_deps["query"].return_value = [{"id": 10}]
        result = await rename_conversation(42, 10, "New Title")
        assert result is True
        query = mock_deps["query"].call_args[0][0]
        assert "UPDATE conversations" in query

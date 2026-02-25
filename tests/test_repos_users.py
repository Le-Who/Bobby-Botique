"""Tests for app.repos.users — auth, state, feedback."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock, PropertyMock
from dataclasses import dataclass


@dataclass
class _MockSettings:
    ADMIN_ID: int = 123


@pytest.fixture
def mock_deps():
    """Patch db_manager, db_query, and settings for users module."""
    from app.repos import users

    mock_lock = AsyncMock()
    mock_lock.__aenter__.return_value = None
    mock_lock.__aexit__.return_value = None

    with patch.object(users, "db_query", new_callable=AsyncMock) as m_query, \
         patch.object(users, "db_manager") as m_mgr, \
         patch.object(users, "reconnect_database", new_callable=AsyncMock), \
         patch.object(users, "set_user_context", new_callable=AsyncMock), \
         patch.object(users, "clear_user_context", new_callable=AsyncMock), \
         patch.object(users, "settings", _MockSettings()):
        m_mgr._cache_lock = mock_lock
        m_mgr._user_auth_cache = {}
        m_mgr.is_connected = True

        mock_conn = MagicMock()
        mock_acq = MagicMock()
        mock_acq.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acq.__aexit__ = AsyncMock(return_value=False)
        m_mgr.pool.acquire.return_value = mock_acq

        yield {"query": m_query, "mgr": m_mgr, "conn": mock_conn}


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------

class TestIsAdmin:
    def test_admin_user(self):
        from app.repos.users import is_admin
        with patch("app.repos.users.settings", _MockSettings(ADMIN_ID=42)):
            assert is_admin(42) is True
            assert is_admin(99) is False


# ---------------------------------------------------------------------------
# is_authorized
# ---------------------------------------------------------------------------

class TestIsAuthorized:
    @pytest.mark.asyncio
    async def test_admin_always_authorized(self, mock_deps):
        from app.repos.users import is_authorized
        assert await is_authorized(123) is True
        mock_deps["query"].assert_not_called()

    @pytest.mark.asyncio
    async def test_authorized_user_from_db(self, mock_deps):
        from app.repos.users import is_authorized
        mock_deps["query"].return_value = [{"is_authorized": 1}]
        assert await is_authorized(999) is True

    @pytest.mark.asyncio
    async def test_unauthorized_user(self, mock_deps):
        from app.repos.users import is_authorized
        mock_deps["query"].return_value = [{"is_authorized": 0}]
        assert await is_authorized(999) is False

    @pytest.mark.asyncio
    async def test_cached_authorization(self, mock_deps):
        from app.repos.users import is_authorized
        mock_deps["mgr"]._user_auth_cache = {999: True}
        assert await is_authorized(999) is True
        mock_deps["query"].assert_not_called()


# ---------------------------------------------------------------------------
# invalidate_user_auth_cache
# ---------------------------------------------------------------------------

class TestInvalidateCache:
    @pytest.mark.asyncio
    async def test_removes_from_cache(self, mock_deps):
        from app.repos.users import invalidate_user_auth_cache
        mock_deps["mgr"]._user_auth_cache = {42: True, 99: False}
        await invalidate_user_auth_cache(42)
        assert 42 not in mock_deps["mgr"]._user_auth_cache
        assert 99 in mock_deps["mgr"]._user_auth_cache

    @pytest.mark.asyncio
    async def test_noop_if_not_cached(self, mock_deps):
        from app.repos.users import invalidate_user_auth_cache
        await invalidate_user_auth_cache(999)  # Should not raise


# ---------------------------------------------------------------------------
# load_user_state
# ---------------------------------------------------------------------------

class TestLoadUserState:
    @pytest.mark.asyncio
    async def test_returns_state_dict(self, mock_deps):
        from app.repos.users import load_user_state
        mock_deps["query"].return_value = [{
            "document_mode": True,
            "selected_document_id": 5,
            "awaiting_custom_role_input": False,
            "generated_role": None,
            "last_custom_role_prompt": None,
            "generating_custom_role": False,
            "last_sent_message_text": "hello",
        }]
        result = await load_user_state(42)
        assert result is not None
        assert result["document_mode"] is True
        assert result["selected_document_id"] == 5

    @pytest.mark.asyncio
    async def test_returns_none_if_no_state(self, mock_deps):
        from app.repos.users import load_user_state
        mock_deps["query"].return_value = []
        assert await load_user_state(42) is None


# ---------------------------------------------------------------------------
# save_user_state
# ---------------------------------------------------------------------------

class TestSaveUserState:
    @pytest.mark.asyncio
    async def test_upsert_query_called(self, mock_deps):
        from app.repos.users import save_user_state
        await save_user_state(42, document_mode=True)
        mock_deps["query"].assert_called_once()
        query = mock_deps["query"].call_args[0][0]
        assert "INSERT INTO user_state" in query
        assert "ON CONFLICT" in query


# ---------------------------------------------------------------------------
# save_feedback
# ---------------------------------------------------------------------------

class TestSaveFeedback:
    @pytest.mark.asyncio
    async def test_insert_called(self, mock_deps):
        from app.repos.users import save_feedback
        await save_feedback(42, 100, "up")
        mock_deps["query"].assert_called_once()
        query, params = mock_deps["query"].call_args[0]
        assert "INSERT INTO feedback" in query
        assert params == (42, 100, "up")

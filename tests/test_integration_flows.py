"""
Integration tests for cross-module flows.

These tests mock only external boundaries (database, AI APIs, Telegram)
and verify that the internal business logic chain works end-to-end.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, datetime


# ── Flow 1: AI response lifecycle ──────────────────────────────────────────


class TestAIResponseLifecycle:
    """Tests the full flow: user message → provider routing → AI call → response."""

    @pytest.mark.asyncio
    async def test_gemini_response_lifecycle(self) -> None:
        """Gemini model routes through GeminiProvider and returns AIResponse."""
        from app.ai_provider import get_ai_response, AIResponse

        mock_response = AIResponse(
            text="Hello from Gemini!",
            token_count=15,
            success=True,
            provider="gemini",
            model="gemini-2.0-flash",
        )

        with patch("app.ai_provider.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="test-key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.0-flash",
            )

        assert text == "Hello from Gemini!"
        assert tokens == 15
        mock_factory.assert_called_once_with("gemini-2.0-flash", "test-key")
        mock_provider.get_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_openrouter_response_lifecycle(self) -> None:
        """OpenRouter model (contains /) routes through OpenRouterProvider."""
        from app.ai_provider import get_ai_response, AIResponse

        mock_response = AIResponse(
            text="Hello from GPT-4!",
            token_count=20,
            success=True,
            provider="openrouter",
            model="openai/gpt-4o",
        )

        with patch("app.ai_provider.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="test-key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="openai/gpt-4o",
            )

        assert text == "Hello from GPT-4!"
        assert tokens == 20

    @pytest.mark.asyncio
    async def test_error_response_returns_none_tokens(self) -> None:
        """On error, text is error message and tokens is None."""
        from app.ai_provider import get_ai_response, AIResponse

        mock_response = AIResponse(
            text="❌ API error",
            token_count=0,
            success=False,
            error_message="Rate limit exceeded",
        )

        with patch("app.ai_provider.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.0-flash",
            )

        assert "❌" in text
        assert tokens is None


# ── Flow 2: Key rotation ──────────────────────────────────────────────────


class TestKeyRotationFlow:
    """Tests the DailyKeyManager key rotation: get → exhaust → rotate."""

    @pytest.fixture
    def mock_db(self):
        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_query:
            yield mock_query

    @pytest.mark.asyncio
    async def test_daily_key_manager_get_available(self, mock_db) -> None:
        """DailyKeyManager returns the least-used key."""
        from app.repos.keys import DailyKeyManager

        km = DailyKeyManager("api_keys", "key_usage")
        mock_db.return_value = [
            {"key_hash": "abc123", "api_key": "enc_key_1", "request_count": 5}
        ]

        with patch("app.repos.keys.safe_decrypt", return_value="decrypted_key"):
            result = await km.get_available_key("gemini-2.0-flash")
        assert result["key_hash"] == "abc123"
        assert result["api_key"] == "decrypted_key"
        # Verify the SQL uses ORDER BY request_count ASC
        sql = mock_db.call_args[0][0]
        assert "ORDER BY" in sql

    @pytest.mark.asyncio
    async def test_daily_key_manager_all_exhausted(self, mock_db) -> None:
        """Returns None when all keys exceed threshold."""
        from app.repos.keys import DailyKeyManager

        km = DailyKeyManager("api_keys", "key_usage")
        mock_db.return_value = []  # No keys under limit

        result = await km.get_available_key("gemini-2.0-flash")
        assert result is None

    @pytest.mark.asyncio
    async def test_daily_key_manager_increment(self, mock_db) -> None:
        """Increment usage UPSERTs into the usage table."""
        from app.repos.keys import DailyKeyManager

        km = DailyKeyManager("api_keys", "key_usage")
        mock_db.return_value = [{"request_count": 6}]

        result = await km.increment_usage("abc123", "gemini-2.0-flash")
        assert result[0]["request_count"] == 6
        sql = mock_db.call_args[0][0]
        assert "INSERT INTO key_usage" in sql

    @pytest.mark.asyncio
    async def test_daily_key_manager_is_available_under_limit(self, mock_db) -> None:
        """Key is available when usage < threshold."""
        from app.repos.keys import DailyKeyManager
        from app.config import settings

        km = DailyKeyManager("api_keys", "key_usage")
        mock_db.return_value = [{"request_count": 10}]
        daily_limit = 100
        threshold = daily_limit * settings.LIMIT_THRESHOLD_PERCENT

        result = await km.is_key_available("abc123", "gemini-2.0-flash", daily_limit)
        # 10 < threshold (e.g., 90% of 100 = 90)
        assert result is True

    @pytest.mark.asyncio
    async def test_daily_key_manager_is_available_over_limit(self, mock_db) -> None:
        """Key is NOT available when usage >= threshold."""
        from app.repos.keys import DailyKeyManager
        from app.config import settings

        km = DailyKeyManager("api_keys", "key_usage")
        mock_db.return_value = [{"request_count": 95}]
        daily_limit = 100

        result = await km.is_key_available("abc123", "gemini-2.0-flash", daily_limit)
        assert result is False

    @pytest.mark.asyncio
    async def test_monthly_key_manager_get_available(self, mock_db) -> None:
        """MonthlyKeyManager returns the least-used key under credit limit."""
        from app.repos.keys import MonthlyKeyManager

        km = MonthlyKeyManager(
            keys_table="tavily_api_keys",
            usage_table="tavily_key_usage",
            credit_limit=1000,
            threshold_percent=0.9,
        )
        mock_db.return_value = [
            {"key_hash": "tav123", "api_key": "enc_tavily_1", "credit_usage": 500}
        ]

        with patch("app.repos.keys.safe_decrypt", return_value="decrypted_tavily"):
            result = await km.get_available_key()
        assert result["key_hash"] == "tav123"

    @pytest.mark.asyncio
    async def test_monthly_key_manager_all_exhausted(self, mock_db) -> None:
        """MonthlyKeyManager returns None when all keys exceed credit limit."""
        from app.repos.keys import MonthlyKeyManager

        km = MonthlyKeyManager(
            keys_table="tavily_api_keys",
            usage_table="tavily_key_usage",
            credit_limit=1000,
            threshold_percent=0.9,
        )
        mock_db.return_value = []

        result = await km.get_available_key()
        assert result is None


# ── Flow 3: Conversation lifecycle ─────────────────────────────────────────


class TestConversationLifecycle:
    """Tests conversation CRUD: create → list → rename → delete."""

    @pytest.fixture
    def mock_db(self):
        with (
            patch("app.repos.conversations.db_query", new_callable=AsyncMock) as mock_query,
            patch("app.repos.conversations.db_manager") as mock_mgr,
        ):
            mock_mgr.is_connected = True
            mock_mgr._cache_lock = MagicMock()
            mock_mgr._cache_lock.__aenter__ = AsyncMock(return_value=None)
            mock_mgr._cache_lock.__aexit__ = AsyncMock(return_value=None)
            # Mock pool.acquire() → conn with conn.transaction() support
            mock_conn = MagicMock()
            mock_txn = MagicMock()
            mock_txn.__aenter__ = AsyncMock(return_value=None)
            mock_txn.__aexit__ = AsyncMock(return_value=None)
            mock_conn.transaction.return_value = mock_txn
            mock_mgr.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_mgr.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
            yield mock_query

    @pytest.mark.asyncio
    async def test_create_then_list(self, mock_db) -> None:
        """After saving a conversation, listing returns it."""
        from app.repos.conversations import save_conversation, get_user_conversations

        # Mock get_user_chat (imported from chats module)
        mock_chat = MagicMock()
        mock_chat.history = [{"role": "user", "parts": ["hello"]}]
        mock_chat.token_count = 100

        # save_conversation returns ID 42
        mock_db.return_value = [{"id": 42}]
        with patch(
            "app.repos.chats.get_user_chat",
            new_callable=AsyncMock,
            return_value=mock_chat,
        ):
            result = await save_conversation(1, "Test Chat", "system", None)
        assert result == 42

        # Now list conversations
        mock_db.return_value = [
            {
                "id": 42, "title": "Test Chat", "role_type": "system",
                "role_id": None, "summary": None, "token_budget": None,
                "created_at": "2024-01-01", "role_title": "Assistant",
                "user_role_title": None,
            }
        ]
        convs = await get_user_conversations(1)
        assert len(convs) == 1
        assert convs[0]["id"] == 42
        assert convs[0]["title"] == "Test Chat"

    @pytest.mark.asyncio
    async def test_rename(self, mock_db) -> None:
        """Renaming a conversation updates the title."""
        from app.repos.conversations import rename_conversation

        mock_db.return_value = None
        result = await rename_conversation(1, 42, "New Title")
        sql = mock_db.call_args[0][0]
        assert "UPDATE conversations" in sql
        assert "title" in sql.lower()

    @pytest.mark.asyncio
    async def test_delete(self, mock_db) -> None:
        """Deleting a conversation removes it atomically within a transaction."""
        from app.repos.conversations import delete_conversation

        # First call: DELETE messages; second call: DELETE conversation RETURNING id
        mock_db.side_effect = [
            None,               # DELETE messages
            [{"id": 42}],       # DELETE conversation RETURNING id
        ]
        result = await delete_conversation(1, 42)
        assert result is True
        # Last call should be DELETE ... RETURNING
        sql = mock_db.call_args_list[-1][0][0]
        assert "DELETE FROM conversations" in sql
        assert "RETURNING" in sql

    @pytest.mark.asyncio
    async def test_save_preserves_history(self, mock_db) -> None:
        """Saving a conversation serializes the chat history."""
        from app.repos.conversations import save_conversation

        mock_chat = MagicMock()
        mock_chat.history = [
            {"role": "user", "parts": ["What is Python?"]},
            {"role": "model", "parts": ["Python is a programming language."]},
        ]
        mock_chat.token_count = 250

        mock_db.return_value = [{"id": 99}]
        with patch(
            "app.repos.chats.get_user_chat",
            new_callable=AsyncMock,
            return_value=mock_chat,
        ):
            result = await save_conversation(1, "Python Chat")
        assert result == 99
        # Verify the stored proc was called
        sql = mock_db.call_args[0][0]
        assert "conversation" in sql.lower()


# ── Flow 4: User auth chain ───────────────────────────────────────────────


class TestUserAuthChain:
    """Tests user authorization: admin bypass, DB lookup, cache."""

    @pytest.fixture
    def mock_db(self):
        with (
            patch("app.repos.users.db_query", new_callable=AsyncMock) as mock_query,
            patch("app.repos.users.db_manager") as mock_mgr,
            patch("app.repos.users.set_user_context", new_callable=AsyncMock),
            patch("app.repos.users.clear_user_context", new_callable=AsyncMock),
            patch("app.repos.users.reconnect_database", new_callable=AsyncMock),
        ):
            mock_mgr.is_connected = True
            mock_mgr._user_auth_cache = {}
            mock_mgr._cache_lock = MagicMock()
            mock_mgr._cache_lock.__aenter__ = AsyncMock(return_value=None)
            mock_mgr._cache_lock.__aexit__ = AsyncMock(return_value=None)
            # Mock the pool.acquire context manager
            mock_conn = AsyncMock()
            mock_mgr.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_mgr.pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
            yield mock_query

    @pytest.mark.asyncio
    async def test_admin_always_authorized(self, mock_db) -> None:
        """Admin user bypasses DB check."""
        from app.repos.users import is_authorized
        from app.config import settings

        result = await is_authorized(settings.ADMIN_ID)
        assert result is True
        mock_db.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authorized_from_db(self, mock_db) -> None:
        """Non-admin user authorized via DB lookup."""
        from app.repos.users import is_authorized

        mock_db.return_value = [{"is_authorized": 1}]
        result = await is_authorized(999)
        assert result is True

    @pytest.mark.asyncio
    async def test_unauthorized_user(self, mock_db) -> None:
        """Unknown user is not authorized."""
        from app.repos.users import is_authorized

        mock_db.return_value = []
        result = await is_authorized(88888)
        assert not result

    @pytest.mark.asyncio
    async def test_auth_result_is_cached(self, mock_db) -> None:
        """Second auth check uses cache, not DB."""
        from app.repos.users import is_authorized

        mock_db.return_value = [{"is_authorized": 1}]

        # First call hits DB
        await is_authorized(999)
        assert mock_db.await_count == 1

        # Second call should use cache
        await is_authorized(999)
        assert mock_db.await_count == 1  # Still 1 — cache hit

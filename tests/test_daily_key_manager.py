"""
Tests for DailyKeyManager in app/repos/keys.py.
"""

from unittest.mock import AsyncMock, patch

import pytest

# ─── DailyKeyManager unit tests ────────────────────────────────────────────


class TestDailyKeyManager:
    """Tests for the generic DailyKeyManager key rotation engine."""

    def _make_manager(self):
        from app.repos.keys import DailyKeyManager

        return DailyKeyManager("test_keys", "test_usage")

    @pytest.mark.asyncio
    async def test_get_available_key_happy_path(self):
        """Should return the least-used key."""
        mgr = self._make_manager()
        mock_result = [{"key_hash": "hash1", "api_key": "key1", "request_count": 5}]

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = mock_result
            result = await mgr.get_available_key("gemini-3.1-flash-lite")

        assert result == {"key_hash": "hash1", "api_key": "key1"}
        call_args = mock_db.call_args
        assert "test_keys" in call_args[0][0]
        assert "test_usage" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_get_available_key_empty_table(self):
        """Should return None when no keys exist."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = []
            result = await mgr.get_available_key("gemini-3.1-flash-lite")

        assert result is None

    @pytest.mark.asyncio
    async def test_increment_usage_upsert(self):
        """Should UPSERT and return the new count."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = [{"request_count": 42}]
            result = await mgr.increment_usage("hash1", "gemini-3.1-flash-lite")

        assert result[0]["request_count"] == 42
        sql = mock_db.call_args[0][0]
        assert "INSERT INTO test_usage" in sql
        assert "ON CONFLICT" in sql

    @pytest.mark.asyncio
    async def test_is_key_available_under_limit(self):
        """Should return True when usage is under threshold."""
        mgr = self._make_manager()

        with (
            patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db,
            patch("app.repos.keys.settings") as mock_settings,
        ):
            mock_settings.LIMIT_THRESHOLD_PERCENT = 0.9
            mock_db.return_value = [{"request_count": 50}]
            result = await mgr.is_key_available("hash1", "model", 100)

        assert result is True  # 50 < 100 * 0.9 = 90

    @pytest.mark.asyncio
    async def test_is_key_available_over_limit(self):
        """Should return False when usage exceeds threshold."""
        mgr = self._make_manager()

        with (
            patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db,
            patch("app.repos.keys.settings") as mock_settings,
        ):
            mock_settings.LIMIT_THRESHOLD_PERCENT = 0.9
            mock_db.return_value = [{"request_count": 95}]
            result = await mgr.is_key_available("hash1", "model", 100)

        assert result is False  # 95 >= 100 * 0.9 = 90

    @pytest.mark.asyncio
    async def test_is_key_available_no_limit(self):
        """Should return True when no daily limit is set."""
        mgr = self._make_manager()
        # No db_query needed — should short-circuit
        result = await mgr.is_key_available("hash1", "model", None)
        assert result is True

    @pytest.mark.asyncio
    async def test_is_key_available_no_usage_record(self):
        """Should return True when key has no usage record yet."""
        mgr = self._make_manager()

        with (
            patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db,
            patch("app.repos.keys.settings") as mock_settings,
        ):
            mock_settings.LIMIT_THRESHOLD_PERCENT = 0.9
            mock_db.return_value = []  # No usage record
            result = await mgr.is_key_available("hash1", "model", 100)

        assert result is True  # 0 < 90

    @pytest.mark.asyncio
    async def test_get_fresh_available_key_with_limit(self):
        """Should skip exhausted keys and return first available."""
        mgr = self._make_manager()

        with (
            patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db,
            patch("app.repos.keys.settings") as mock_settings,
        ):
            mock_settings.LIMIT_THRESHOLD_PERCENT = 0.9
            mock_db.return_value = [
                {"key_hash": "h1", "api_key": "k1", "request_count": 95},  # over
                {"key_hash": "h2", "api_key": "k2", "request_count": 10},  # under
            ]
            result = await mgr.get_fresh_available_key("model", 100)

        assert result == {"key_hash": "h2", "api_key": "k2"}

    @pytest.mark.asyncio
    async def test_get_fresh_available_key_all_exhausted(self):
        """Should return None when all keys are over threshold."""
        mgr = self._make_manager()

        with (
            patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db,
            patch("app.repos.keys.settings") as mock_settings,
        ):
            mock_settings.LIMIT_THRESHOLD_PERCENT = 0.9
            mock_db.return_value = [
                {"key_hash": "h1", "api_key": "k1", "request_count": 95},
                {"key_hash": "h2", "api_key": "k2", "request_count": 91},
            ]
            result = await mgr.get_fresh_available_key("model", 100)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_fresh_available_key_no_limit(self):
        """Should return any key when no daily limit is set."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = [{"key_hash": "h1", "api_key": "k1"}]
            result = await mgr.get_fresh_available_key("model", None)

        assert result == {"key_hash": "h1", "api_key": "k1"}

    @pytest.mark.asyncio
    async def test_get_fresh_available_key_empty_table(self):
        """Should return None when no keys exist."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = []
            result = await mgr.get_fresh_available_key("model", 100)

        assert result is None

    def test_table_names_are_parameterized(self):
        """Gemini and OpenRouter singletons should use different tables."""
        from app.repos.keys import _gemini_km, _openrouter_km

        assert _gemini_km.keys_table == "api_keys"
        assert _gemini_km.usage_table == "key_usage"
        assert _openrouter_km.keys_table == "openrouter_api_keys"
        assert _openrouter_km.usage_table == "openrouter_key_usage"


# ─── MonthlyKeyManager unit tests ──────────────────────────────────────────


class TestMonthlyKeyManager:
    """Tests for the monthly-credit MonthlyKeyManager engine."""

    def _make_manager(self):
        from app.repos.keys import MonthlyKeyManager

        return MonthlyKeyManager(
            keys_table="test_monthly_keys",
            usage_table="test_monthly_usage",
            credit_limit=1000,
            threshold_percent=0.9,
        )

    @pytest.mark.asyncio
    async def test_get_available_key_happy_path(self):
        """Should return the least-used key under threshold."""
        mgr = self._make_manager()
        mock_result = [
            {"key_hash": "h1", "api_key": "k1", "credit_usage": 200},
            {"key_hash": "h2", "api_key": "k2", "credit_usage": 500},
        ]

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = mock_result
            result = await mgr.get_available_key()

        assert result == {"key_hash": "h1", "api_key": "k1"}

    @pytest.mark.asyncio
    async def test_get_available_key_all_exhausted(self):
        """Should return None when all keys exceed threshold."""
        mgr = self._make_manager()
        mock_result = [
            {"key_hash": "h1", "api_key": "k1", "credit_usage": 950},
            {"key_hash": "h2", "api_key": "k2", "credit_usage": 920},
        ]

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = mock_result
            result = await mgr.get_available_key()

        assert result is None  # Both >= 1000 * 0.9 = 900

    @pytest.mark.asyncio
    async def test_get_available_key_empty_table(self):
        """Should return None when no keys exist."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            mock_db.return_value = []
            result = await mgr.get_available_key()

        assert result is None

    @pytest.mark.asyncio
    async def test_increment_usage_upsert(self):
        """Should UPSERT and add cost to monthly credit."""
        mgr = self._make_manager()

        with patch("app.repos.keys.db_query", new_callable=AsyncMock) as mock_db:
            await mgr.increment_usage("hash1", 5)

        sql = mock_db.call_args[0][0]
        assert "INSERT INTO test_monthly_usage" in sql
        assert "ON CONFLICT" in sql

    def test_tavily_singleton_config(self):
        """Tavily singleton should use correct tables and settings."""
        from app.repos.keys import _tavily_km

        assert _tavily_km.keys_table == "tavily_api_keys"
        assert _tavily_km.usage_table == "tavily_key_usage"

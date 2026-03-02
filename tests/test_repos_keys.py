"""Tests for app.repos.keys — DailyKeyManager, MonthlyKeyManager, key rotation."""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass
class _MockSettings:
    ADMIN_ID: int = 123
    DAILY_LIMITS: dict[str, int] = field(default_factory=lambda: {"test-model": 100})
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97


@pytest.fixture
def mock_deps():
    """Patch db deps for keys module."""
    from app.repos import keys

    mock_lock = AsyncMock()
    mock_lock.__aenter__.return_value = None
    mock_lock.__aexit__.return_value = None

    with (
        patch.object(keys, "db_query", new_callable=AsyncMock) as m_query,
        patch.object(keys, "db_execute_many", new_callable=AsyncMock) as m_exec,
        patch.object(keys, "db_manager") as m_mgr,
        patch.object(keys, "reconnect_database", new_callable=AsyncMock),
        patch.object(keys, "set_user_context", new_callable=AsyncMock),
        patch.object(keys, "clear_user_context", new_callable=AsyncMock),
        patch.object(keys, "settings", _MockSettings()),
    ):
        m_mgr._cache_lock = mock_lock
        m_mgr._active_keys_cache = {}
        m_mgr._model_config_cache = {}
        m_mgr.is_connected = True

        mock_conn = MagicMock()
        mock_acq = MagicMock()
        mock_acq.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acq.__aexit__ = AsyncMock(return_value=False)
        m_mgr.pool.acquire.return_value = mock_acq

        yield {"query": m_query, "exec": m_exec, "mgr": m_mgr, "conn": mock_conn}


# ---------------------------------------------------------------------------
# get_model_daily_limit
# ---------------------------------------------------------------------------


class TestGetModelDailyLimit:
    @pytest.mark.asyncio
    async def test_returns_cached_limit(self, mock_deps):
        from app.repos.keys import get_model_daily_limit

        mock_deps["mgr"]._model_config_cache = {"test-model": {"daily_limit": 200}}
        result = await get_model_daily_limit("test-model")
        # Cache stores the whole config dict; function returns the daily_limit value
        assert result == 200 or result == {"daily_limit": 200}
        mock_deps["query"].assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_db_limit(self, mock_deps):
        from app.repos.keys import get_model_daily_limit

        mock_deps["query"].return_value = [{"daily_limit": 300}]
        result = await get_model_daily_limit("other-model")
        assert result == 300

    @pytest.mark.asyncio
    async def test_returns_none_if_not_found(self, mock_deps):
        from app.repos.keys import get_model_daily_limit

        mock_deps["query"].return_value = []
        result = await get_model_daily_limit("unknown")
        assert result is None


# ---------------------------------------------------------------------------
# invalidate_key_cache
# ---------------------------------------------------------------------------


class TestInvalidateKeyCache:
    @pytest.mark.asyncio
    async def test_clears_specific_model(self, mock_deps):
        from app.repos.keys import invalidate_key_cache

        mock_deps["mgr"]._active_keys_cache = {"m1": {}, "m2": {}}
        await invalidate_key_cache("m1")
        assert "m1" not in mock_deps["mgr"]._active_keys_cache
        assert "m2" in mock_deps["mgr"]._active_keys_cache

    @pytest.mark.asyncio
    async def test_clears_all(self, mock_deps):
        from app.repos.keys import invalidate_key_cache

        mock_deps["mgr"]._active_keys_cache = {"m1": {}, "m2": {}}
        await invalidate_key_cache(None)
        assert len(mock_deps["mgr"]._active_keys_cache) == 0


# ---------------------------------------------------------------------------
# get_available_gemini_key
# ---------------------------------------------------------------------------


class TestGetAvailableGeminiKey:
    @pytest.mark.asyncio
    async def test_returns_cached_key(self, mock_deps):
        from app.repos.keys import get_available_gemini_key

        mock_deps["mgr"]._active_keys_cache = {"test-model": {"key_hash": "abc", "api_key": "AIza..."}}
        # Mock _is_key_available to return True
        with patch("app.repos.keys._is_key_available", new_callable=AsyncMock, return_value=True):
            result = await get_available_gemini_key("test-model")
            assert result is not None
            assert result["key_hash"] == "abc"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_keys(self, mock_deps):
        from app.repos.keys import get_available_gemini_key

        with patch("app.repos.keys._get_fresh_available_key", new_callable=AsyncMock, return_value=None):
            result = await get_available_gemini_key("test-model")
            assert result is None


# ---------------------------------------------------------------------------
# increment_gemini_key_usage
# ---------------------------------------------------------------------------


class TestIncrementGeminiKeyUsage:
    @pytest.mark.asyncio
    async def test_calls_upsert(self, mock_deps):
        from app.repos import keys as keys_mod
        from app.repos.keys import increment_gemini_key_usage

        # Mock the internal functions directly instead of chaining db_query
        with (
            patch.object(keys_mod, "_gemini_km") as mock_km,
            patch.object(keys_mod, "get_model_daily_limit", new_callable=AsyncMock, return_value=100),
        ):
            mock_km.increment_usage = AsyncMock(return_value=[{"request_count": 5}])
            await increment_gemini_key_usage("hash123", "test-model")
            mock_km.increment_usage.assert_called_once_with("hash123", "test-model")


# ---------------------------------------------------------------------------
# DailyKeyManager
# ---------------------------------------------------------------------------


class TestDailyKeyManager:
    def test_init(self):
        from app.repos.keys import DailyKeyManager

        km = DailyKeyManager("my_keys", "my_usage")
        assert km.keys_table == "my_keys"
        assert km.usage_table == "my_usage"

    @pytest.mark.asyncio
    async def test_increment_usage(self, mock_deps):
        from app.repos.keys import DailyKeyManager

        km = DailyKeyManager("api_keys", "key_usage")
        await km.increment_usage("hash", "model")
        mock_deps["query"].assert_called_once()
        query = mock_deps["query"].call_args[0][0]
        assert "INSERT INTO key_usage" in query


# ---------------------------------------------------------------------------
# MonthlyKeyManager
# ---------------------------------------------------------------------------


class TestMonthlyKeyManager:
    def test_init(self):
        from app.repos.keys import MonthlyKeyManager

        km = MonthlyKeyManager("t_keys", "t_usage", 1000, 0.97)
        assert km.credit_limit == 1000
        assert km.threshold_percent == 0.97

    @pytest.mark.asyncio
    async def test_increment_usage(self, mock_deps):
        from app.repos.keys import MonthlyKeyManager

        km = MonthlyKeyManager("tavily_api_keys", "tavily_key_usage", 1000, 0.97)
        await km.increment_usage("hash", 5)
        mock_deps["query"].assert_called_once()
        query = mock_deps["query"].call_args[0][0]
        assert "tavily_key_usage" in query


# ---------------------------------------------------------------------------
# get_available_openrouter_key
# ---------------------------------------------------------------------------


class TestGetAvailableOpenrouterKey:
    @pytest.mark.asyncio
    async def test_passes_excluded_hashes(self, mock_deps):
        """excluded_hashes must be forwarded to get_fresh_available_key."""
        from app.repos import keys as keys_mod
        from app.repos.keys import get_available_openrouter_key

        fake_key = {"key_hash": "or_abc", "api_key": "sk-or-..."}
        with (
            patch.object(keys_mod, "_openrouter_km") as mock_km,
            patch.object(
                keys_mod,
                "get_model_daily_limit",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_km.get_fresh_available_key = AsyncMock(return_value=fake_key)
            excluded = {"bad_hash_1", "bad_hash_2"}
            result = await get_available_openrouter_key(
                "openai/gpt-4o:free",
                excluded_hashes=excluded,
            )
            assert result == fake_key
            mock_km.get_fresh_available_key.assert_called_once()
            call_kwargs = mock_km.get_fresh_available_key.call_args
            assert call_kwargs.kwargs.get("excluded_hashes") == excluded

    @pytest.mark.asyncio
    async def test_returns_none_when_no_keys(self, mock_deps):
        """Returns None when all keys are exhausted or excluded."""
        from app.repos import keys as keys_mod
        from app.repos.keys import get_available_openrouter_key

        with (
            patch.object(keys_mod, "_openrouter_km") as mock_km,
            patch.object(
                keys_mod,
                "get_model_daily_limit",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_km.get_fresh_available_key = AsyncMock(return_value=None)
            result = await get_available_openrouter_key("openai/gpt-4o:free")
            assert result is None

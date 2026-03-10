import hashlib
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@dataclass
class MockSettings:
    TAVILY_API_KEYS: list[str] = field(default_factory=list)
    ADMIN_ID: int = 123
    DATABASE_URL: str = "postgres://"
    DAILY_LIMITS: dict = field(default_factory=dict)
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97


@pytest.mark.asyncio
async def test_force_update_tavily_keys():
    """Test that force_update_tavily_keys deletes old keys and inserts encrypted ones."""
    test_keys = ["key1", "key2", "key3"]
    expected_hashes = [hashlib.sha256(k.encode()).hexdigest() for k in test_keys]
    mock_settings = MockSettings(TAVILY_API_KEYS=test_keys)

    from app.repos import keys as keys_mod

    mock_lock = MagicMock()
    mock_lock.__aenter__ = AsyncMock(return_value=None)
    mock_lock.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.config.get_settings", return_value=mock_settings),
        patch.object(keys_mod, "encrypt_api_key", side_effect=lambda k: f"enc_{k}"),
        patch.object(keys_mod, "db_query", new_callable=AsyncMock) as mock_db_query,
        patch.object(keys_mod, "db_execute_many", new_callable=AsyncMock) as mock_db_exec_many,
        patch.object(keys_mod, "db_manager") as mock_db_manager,
    ):
        mock_db_manager._cache_lock = mock_lock
        mock_db_manager._active_keys_cache = {}

        result = await keys_mod.force_update_tavily_keys()

        assert result is True

        # Verify DELETE calls
        calls = [c[0][0] for c in mock_db_query.call_args_list]
        assert "DELETE FROM tavily_api_keys" in calls
        assert "DELETE FROM tavily_key_usage" in calls

        # Verify INSERT with encrypted keys
        mock_db_exec_many.assert_called_once()
        query, data = mock_db_exec_many.call_args[0]
        assert "INSERT INTO tavily_api_keys" in query
        assert len(data) == 3
        for i, (key_hash, encrypted_key) in enumerate(data):
            assert key_hash == expected_hashes[i]
            assert encrypted_key == f"enc_{test_keys[i]}"


@pytest.mark.asyncio
async def test_force_update_tavily_keys_empty():
    mock_settings = MockSettings(TAVILY_API_KEYS=[])

    from app.repos import keys as keys_mod

    with (
        patch("app.config.get_settings", return_value=mock_settings),
        patch.object(keys_mod, "db_query", new_callable=AsyncMock) as mock_db_query,
        patch.object(keys_mod, "db_execute_many", new_callable=AsyncMock),
        patch.object(keys_mod, "db_manager") as mock_db_manager,
    ):
        mock_lock = MagicMock()
        mock_lock.__aenter__ = AsyncMock(return_value=None)
        mock_lock.__aexit__ = AsyncMock(return_value=None)
        mock_db_manager._cache_lock = mock_lock

        result = await keys_mod.force_update_tavily_keys()

        assert result is False
        assert mock_db_query.call_count == 0

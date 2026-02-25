import pytest
import hashlib
import sys
from unittest.mock import MagicMock, patch, AsyncMock
from typing import List
from dataclasses import dataclass, field

# Ensure app.config is loaded before patching
try:
    pass
except ImportError:
    pass


# Removed manual sys.modules intervention which broke downstream module evaluations


@dataclass
class MockSettings:
    TAVILY_API_KEYS: List[str] = field(default_factory=list)
    ADMIN_ID: int = 123
    DATABASE_URL: str = "postgres://"
    DAILY_LIMITS: dict = field(default_factory=dict)
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97


@pytest.mark.asyncio
async def test_force_update_tavily_keys():
    # Setup
    test_keys = ["key1", "key2", "key3"]
    expected_data = [(hashlib.sha256(k.encode()).hexdigest(), k) for k in test_keys]

    mock_settings = MockSettings(TAVILY_API_KEYS=test_keys)

    if "app.config" not in sys.modules:
        sys.modules["app.config"] = MagicMock()

    with patch("app.config.get_settings", return_value=mock_settings):
        from app import database

        # Create mock connection with transaction support
        # transaction() is called synchronously, returns an async context manager
        mock_conn = MagicMock()
        mock_txn_ctx = MagicMock()
        mock_txn_ctx.__aenter__ = AsyncMock(return_value=mock_txn_ctx)
        mock_txn_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction.return_value = mock_txn_ctx
        mock_conn.execute = AsyncMock()
        mock_conn.executemany = AsyncMock()

        # pool.acquire() returns a sync object with __aenter__/__aexit__
        mock_acquire_ctx = MagicMock()
        mock_acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_acquire_ctx

        with (
            patch.object(database, "db_manager") as mock_db_manager,
        ):
            mock_db_manager.pool = mock_pool

            # Mock the cache lock
            mock_lock = AsyncMock()
            mock_lock.__aenter__.return_value = None
            mock_lock.__aexit__.return_value = None
            mock_db_manager._cache_lock = mock_lock
            mock_db_manager._active_keys_cache = {}

            # Execute
            result = await database.force_update_tavily_keys()

            # Verify
            assert result is True

            # Verify DELETE + INSERT via conn.execute / conn.executemany
            mock_conn.execute.assert_any_call("DELETE FROM tavily_api_keys")
            mock_conn.execute.assert_any_call("DELETE FROM tavily_key_usage")
            mock_conn.executemany.assert_called_once()

            call_args = mock_conn.executemany.call_args
            query, data = call_args[0]
            assert query == "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)"
            assert len(data) == 3
            assert data == expected_data


@pytest.mark.asyncio
async def test_force_update_tavily_keys_empty():
    mock_settings = MockSettings(TAVILY_API_KEYS=[])

    with patch("app.config.get_settings", return_value=mock_settings):
        from app import database

        with (
            patch.object(database, "db_query", new_callable=AsyncMock) as mock_db_query,
            patch.object(database, "db_execute_many", new_callable=AsyncMock),
            patch.object(database, "db_manager") as mock_db_manager,
        ):
            mock_db_manager._cache_lock = AsyncMock()
            mock_db_manager._cache_lock.__aenter__.return_value = None
            mock_db_manager._cache_lock.__aexit__.return_value = None

            result = await database.force_update_tavily_keys()

            assert result is False
            assert mock_db_query.call_count == 0

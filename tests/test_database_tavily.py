import pytest
import hashlib
import sys
from unittest.mock import MagicMock, patch, AsyncMock
from typing import List
from dataclasses import dataclass, field
import importlib

# Ensure app.config is loaded before patching
try:
    import app.config
except ImportError:
    pass

# Mock dependencies that might be missing in the test environment
# We do this before importing app.database
if "asyncpg" not in sys.modules:
    sys.modules["asyncpg"] = MagicMock()
    sys.modules["asyncpg.pool"] = MagicMock()

if "pytz" not in sys.modules:
    sys.modules["pytz"] = MagicMock()

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
    expected_data = [
        (hashlib.sha256(k.encode()).hexdigest(), k) for k in test_keys
    ]

    mock_settings = MockSettings(TAVILY_API_KEYS=test_keys)

    # We explicitly import app.database to ensure it's available for patching
    # But we want to avoid side effects of import if possible, or handle them.
    # The benchmark script showed we can import it if we mock dependencies.

    # We need to make sure 'app.config' is in sys.modules so patch can find it
    if "app.config" not in sys.modules:
        # Create a mock module if it failed to import
        sys.modules["app.config"] = MagicMock()

    # However, if app.config WAS imported, we patch it.

    # We'll use a nested patch approach.

    # Patch get_settings.
    # Note: We patch where it is imported IF it was imported using 'from ... import ...'
    # app.database does:
    #   from app.config import UTC_TZ, settings
    #   ...
    #   def force_update_tavily_keys():
    #       from app.config import get_settings

    # So we should patch "app.config.get_settings"

    # We also need to patch db_query and db_execute_many in app.database

    with patch("app.config.get_settings", return_value=mock_settings):
        # We need to ensure app.database is imported
        import app.database

        # Now patch the functions inside app.database
        with patch.object(app.database, "db_query", new_callable=AsyncMock) as mock_db_query, \
             patch.object(app.database, "db_execute_many", new_callable=AsyncMock) as mock_db_execute_many, \
             patch.object(app.database, "db_manager") as mock_db_manager:

            # Mock the cache lock
            mock_lock = AsyncMock()
            mock_lock.__aenter__.return_value = None
            mock_lock.__aexit__.return_value = None
            mock_db_manager._cache_lock = mock_lock
            mock_db_manager._active_keys_cache = {}
            mock_db_manager._cache_last_updated = {}

            # Execute
            result = await app.database.force_update_tavily_keys()

            # Verify
            assert result is True

            # Verify DELETE calls
            assert mock_db_query.call_count == 2
            mock_db_query.assert_any_call("DELETE FROM tavily_api_keys")
            mock_db_query.assert_any_call("DELETE FROM tavily_key_usage")

            # Verify INSERT batch call
            assert mock_db_execute_many.call_count == 1

            call_args = mock_db_execute_many.call_args
            query, data = call_args[0]

            assert query == "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2)"
            assert len(data) == 3
            # Sort data to compare if order is not guaranteed (it is guaranteed in list comprehension though)
            assert data == expected_data

@pytest.mark.asyncio
async def test_force_update_tavily_keys_empty():
    mock_settings = MockSettings(TAVILY_API_KEYS=[])

    with patch("app.config.get_settings", return_value=mock_settings):
        import app.database
        with patch.object(app.database, "db_query", new_callable=AsyncMock) as mock_db_query, \
             patch.object(app.database, "db_execute_many", new_callable=AsyncMock) as mock_db_execute_many, \
             patch.object(app.database, "db_manager") as mock_db_manager:

            mock_db_manager._cache_lock = AsyncMock()
            mock_db_manager._cache_lock.__aenter__.return_value = None
            mock_db_manager._cache_lock.__aexit__.return_value = None

            result = await app.database.force_update_tavily_keys()

            assert result is False
            assert mock_db_query.call_count == 0

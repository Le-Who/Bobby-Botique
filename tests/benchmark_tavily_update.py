import asyncio
import time
import sys
import hashlib
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import List, Any
import os

# Mock sys.modules for missing dependencies
sys.modules["pytz"] = MagicMock()
sys.modules["asyncpg"] = MagicMock()
sys.modules["asyncpg.pool"] = MagicMock()

# Mock app.utils.time if needed
mock_utils_time = MagicMock()
sys.modules["app.utils.time"] = mock_utils_time
sys.modules["app.utils"] = MagicMock()

# Mock app.config
mock_config = MagicMock()
sys.modules["app.config"] = mock_config

# Set attributes on mock_config that are imported directly
mock_config.UTC_TZ = "UTC"
mock_config.settings = MagicMock()

@dataclass
class MockSettings:
    TAVILY_API_KEYS: List[str] = field(default_factory=list)
    ADMIN_ID: int = 123456
    DATABASE_URL: str = "postgresql://user:pass@localhost/db"
    DAILY_LIMITS: dict = field(default_factory=dict)
    LIMIT_THRESHOLD_PERCENT: float = 0.95
    TAVILY_MONTHLY_CREDIT_LIMIT: int = 1000
    TAVILY_LIMIT_THRESHOLD_PERCENT: float = 0.97

# Create keys
NUM_KEYS = 1000
mock_keys = [f"tvly-{i}" for i in range(NUM_KEYS)]
mock_settings_obj = MockSettings(TAVILY_API_KEYS=mock_keys)

# Setup get_settings
mock_config.get_settings.return_value = mock_settings_obj

# Now import app.database
import app.database

# Mock the database functions
class BenchmarkStats:
    query_count = 0
    execute_many_count = 0

stats = BenchmarkStats()

async def mock_db_query(query, params=(), retries=3, conn=None):
    stats.query_count += 1
    # Simulate network latency
    await asyncio.sleep(0.001)
    return []

async def mock_db_execute_many(query, params_list, retries=3, conn=None):
    stats.execute_many_count += 1
    # Simulate network latency
    await asyncio.sleep(0.005)
    return

# Patch the functions in app.database module
app.database.db_query = mock_db_query
app.database.db_execute_many = mock_db_execute_many

# Also patch db_manager instance methods just in case
app.database.db_manager.query = mock_db_query
app.database.db_manager.execute_many = mock_db_execute_many
# Also patch the cache lock to avoid errors if event loop is different (though asyncio.run handles it)
app.database.db_manager._cache_lock = asyncio.Lock()
app.database.db_manager._active_keys_cache = {}
app.database.db_manager._cache_last_updated = {}


async def run_benchmark():
    print(f"Starting benchmark with {NUM_KEYS} keys...")
    start_time = time.time()

    # We need to make sure force_update_tavily_keys uses our mocked get_settings
    # The function does: from app.config import get_settings
    # Since we mocked sys.modules['app.config'], it should use our mock.

    try:
        success = await app.database.force_update_tavily_keys()
    except Exception as e:
        print(f"Error during benchmark: {e}")
        import traceback
        traceback.print_exc()
        success = False

    end_time = time.time()
    duration = end_time - start_time

    print(f"Benchmark completed.")
    print(f"Success: {success}")
    print(f"Total time: {duration:.4f} seconds")
    print(f"db_query calls: {stats.query_count}")
    print(f"db_execute_many calls: {stats.execute_many_count}")
    print(f"Total DB interactions: {stats.query_count + stats.execute_many_count}")

if __name__ == "__main__":
    asyncio.run(run_benchmark())

import asyncio
import time
import sys
import os
from unittest.mock import MagicMock

# Mock everything needed
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()
sys.modules['telegram.constants'] = MagicMock()
sys.modules['cachetools'] = MagicMock()
sys.modules['httpx'] = MagicMock()
sys.modules['asyncpg'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['pydantic'] = MagicMock()
sys.modules['PIL'] = MagicMock()
sys.modules['pypdf'] = MagicMock()
sys.modules['docx'] = MagicMock()
sys.modules['marko'] = MagicMock()

# Other mocks to prevent DB connection
sys.modules['app.database'] = MagicMock()
sys.modules['app.config'] = MagicMock()
mock_settings = MagicMock()
mock_settings.DAILY_LIMITS = {"gemini-pro": 1000}
mock_settings.TAVILY_MONTHLY_CREDIT_LIMIT = 1000
sys.modules['app.config'].settings = mock_settings

# Make sure we import menus successfully
try:
    import app.handlers.menus as menus
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)

async def mock_get_system_status_data():
    return {
        "metrics_summary": {
            "total_requests": 1000,
            "average_response_time": 1.5,
            "error_rate": 0.5,
            "cache_hit_rate": 80.0,
            "search_queries": 100,
            "api_calls": {f"api_{i}": i for i in range(50)},
            "model_usage": {f"model_{i}": i for i in range(50)},
            "daily_metrics": {f"date_{i}": {"requests": i, "errors": 0} for i in range(30)},
            "recent_errors": [{"type": "error", "message": f"msg_{i}"} for i in range(10)]
        },
        "gemini": {
            "keys": [{"api_key": f"key_{i}abcd1234", "key_hash": f"hash_{i}"} for i in range(500)],
            "usage_map": {f"hash_{i}": [{"model_name": "gemini-pro", "request_count": i}] for i in range(500)},
            "reset_time": "00:00"
        },
        "tavily": {
            "keys": [{"api_key": f"key_{i}abcd1234", "key_hash": f"hash_{i}"} for i in range(200)],
            "usage_map": {f"hash_{i}": i for i in range(200)}
        }
    }

async def run_benchmark():
    # Patch the data source
    menus.get_system_status_data = mock_get_system_status_data
    menus.datetime = MagicMock()
    menus.datetime.now().strftime.return_value = "12:00:00 UTC"
    menus.format_key_for_display = lambda k: k[:4] + "..." + k[-4:]

    # Warm up
    for _ in range(10):
        await menus.get_metrics_content()

    start = time.perf_counter()
    n = 1000
    for _ in range(n):
        await menus.get_metrics_content()
    end = time.perf_counter()

    print(f"Time taken for {n} iterations (Baseline): {end - start:.4f} seconds")

if __name__ == "__main__":
    asyncio.run(run_benchmark())

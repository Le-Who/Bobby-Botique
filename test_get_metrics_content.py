import sys
import asyncio
from unittest.mock import MagicMock

sys.modules['telegram'] = MagicMock()
sys.modules['pydantic'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['cachetools'] = MagicMock()
sys.modules['asyncpg'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['redis.asyncio'] = MagicMock()
sys.modules['tiktoken'] = MagicMock()
sys.modules['zoneinfo'] = MagicMock()
sys.modules['httpx'] = MagicMock()
sys.modules['google'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['google.genai.types'] = MagicMock()
sys.modules['PIL'] = MagicMock()
sys.modules['docling'] = MagicMock()
sys.modules['docling.document_converter'] = MagicMock()
sys.modules['psycopg2'] = MagicMock()
sys.modules['pdf2image'] = MagicMock()
sys.modules['pypdf'] = MagicMock()
sys.modules['docx'] = MagicMock()
sys.modules['docx.opc.exceptions'] = MagicMock()

import app.config
app.config.ZoneInfo = MagicMock()

from app.handlers.menus import get_metrics_content
from app.metrics import get_system_status_data

async def mock_get_system_status_data():
    return {
        "metrics_summary": {
            "total_requests": 100,
            "average_response_time": 0.5,
            "error_rate": 0.1,
            "cache_hit_rate": 85.0,
            "search_queries": 50,
            "api_calls": {"gemini": 80, "tavily": 20},
            "model_usage": {"model_1": 10},
            "daily_metrics": {"2023-10-01": {"requests": 100, "errors": 1}},
            "recent_errors": [{"type": "TimeoutError", "message": "Connection timed out"}],
        },
        "gemini": {
            "keys": [{"api_key": "key_1", "key_hash": "hash_1"}],
            "usage_map": {"hash_1": [{"model_name": "gemini-pro", "request_count": 50}]},
            "reset_time": "00:00",
        },
        "tavily": {
            "keys": [{"api_key": "key_2", "key_hash": "hash_2"}],
            "usage_map": {"hash_2": 10},
        }
    }

async def run_test():
    import app.handlers.menus
    app.handlers.menus.get_system_status_data = mock_get_system_status_data

    content = await get_metrics_content()
    assert "📊 *Полная сводка системы:*" in content
    assert "Всего запросов: `100`" in content
    print("Regression check passed. The optimized function behaves correctly.")

if __name__ == "__main__":
    asyncio.run(run_test())

import sys
import time
import asyncio
import re
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
            "total_requests": 1000,
            "average_response_time": 0.5,
            "error_rate": 0.1,
            "cache_hit_rate": 85.0,
            "search_queries": 50,
            "api_calls": {"gemini": 800, "tavily": 200},
            "model_usage": {f"model_{i}": i for i in range(1000)},
            "daily_metrics": {f"2023-10-{i:02d}": {"requests": 100, "errors": 1} for i in range(1, 10)},
            "recent_errors": [{"type": "TimeoutError", "message": "Connection timed out"} for _ in range(5)],
        },
        "gemini": {
            "keys": [{"api_key": f"key_{i}", "key_hash": f"hash_{i}"} for i in range(100)],
            "usage_map": {f"hash_{i}": [{"model_name": "gemini-pro", "request_count": 50}] for i in range(100)},
            "reset_time": "00:00",
        },
        "tavily": {
            "keys": [{"api_key": f"key_{i}", "key_hash": f"hash_{i}"} for i in range(50)],
            "usage_map": {f"hash_{i}": 10 for i in range(50)},
        }
    }

async def run_benchmark():
    import app.handlers.menus
    app.handlers.menus.get_system_status_data = mock_get_system_status_data

    # Check correctness
    expected_start = "📊 *Полная сводка системы:*\n\n*🚀 Производительность:*\n• Всего запросов: `1000`"
    content = await get_metrics_content()
    assert content.startswith(expected_start), "Output content does not match expected format"
    # Ensure all parts are included
    assert "*🔌 Использование API:*" in content
    assert "*🤖 Использование моделей:*" in content
    assert "*🔑 Статус ключей Gemini (сегодня):*" in content
    assert "*💳 Кредиты Tavily (текущий месяц):*" in content
    assert "*📈 История за последние дни:*" in content
    assert "*⚠️ Последние ошибки:*" in content
    assert re.search(r"_Обновлено: \d{2}:\d{2}:\d{2} UTC_", content)

    print("Correctness check passed!")

    start_time = time.perf_counter()
    for _ in range(1000):
        await get_metrics_content()
    end_time = time.perf_counter()

    print(f"Elapsed time for 1000 iterations: {end_time - start_time:.4f} seconds")

if __name__ == "__main__":
    asyncio.run(run_benchmark())

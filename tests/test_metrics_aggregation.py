import pytest
from unittest.mock import AsyncMock, patch

# Import app.metrics. Since other tests might have messed with sys.modules,
# we rely on patch to fix imports for this test.
from app.metrics import MetricsCollector


@pytest.mark.asyncio
async def test_load_metrics_from_db_optimized():
    # We patch app.metrics.db.db_query because app.metrics imports database as db
    with patch("app.metrics.db.db_query", new_callable=AsyncMock) as mock_db_query:
        # Mock data for the expected calls to db_query:
        # 1. _ensure_metrics_tables (creates metrics) -> returns None
        # 2. _ensure_metrics_tables (creates error_logs) -> returns None
        # 3. _ensure_metrics_tables (alters error_logs) -> returns None
        # 4. load general metrics -> returns one row
        # 5. load api_calls (NEW) -> returns list of dicts
        # 6. load model_usage (NEW) -> returns list of dicts
        # 7. load daily metrics -> returns list of dicts
        # 8. load error logs -> returns list of dicts

        mock_db_query.side_effect = [
            # _ensure_metrics_tables
            None,
            None,
            None,
            # General metrics
            [
                {
                    "total_requests": 100,
                    "total_time": 50.0,
                    "total_errors": 5,
                    "total_searches": 10,
                    "total_cache_hits": 20,
                    "total_cache_misses": 5,
                }
            ],
            # api_calls (New Aggregated)
            [{"key": "openai", "total": 50}, {"key": "gemini", "total": 30}],
            # model_usage (New Aggregated)
            [{"key": "gpt-4", "total": 20}, {"key": "gemini-pro", "total": 15}],
            # Daily metrics (empty for simplicity)
            [],
            # Error logs (empty)
            [],
        ]

        collector = MetricsCollector()
        await collector._load_metrics_from_db()

        # Verify api_calls
        assert collector.metrics.api_calls == {"openai": 50, "gemini": 30}

        # Verify model_usage
        assert collector.metrics.model_usage == {"gpt-4": 20, "gemini-pro": 15}

        # Verify queries
        # We are interested in calls 4 and 5 (0-indexed)
        calls = mock_db_query.call_args_list

        # assert api_calls query
        api_calls_query = calls[4][0][0]
        assert "FROM metrics, jsonb_each_text(api_calls)" in api_calls_query
        assert "SUM(value::numeric)" in api_calls_query
        assert "GROUP BY key" in api_calls_query

        # assert model_usage query
        model_usage_query = calls[5][0][0]
        assert "FROM metrics, jsonb_each_text(model_usage)" in model_usage_query
        assert "SUM(value::numeric)" in model_usage_query
        assert "GROUP BY key" in model_usage_query

import pytest

pytestmark = pytest.mark.integration
"""Integration tests for metrics JSONB roundtrip — real database.

Validates that the upsert in _save_metrics_to_db and the jsonb_each_text
queries in _load_metrics_from_db work correctly across multiple save cycles.

All tests use transactional rollback — NO data persists after tests complete.
"""

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.integration

# Use dynamic dates so rows always fall within the 30-day query window
TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)

# ---------- SQL copied from metrics.py to test the actual queries ----------

UPSERT_SQL = """
    INSERT INTO metrics (metric_date, request_count, total_response_time, error_count,
                         search_queries, cache_hits, cache_misses, api_calls, model_usage, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
    ON CONFLICT (metric_date) DO UPDATE SET
        request_count = EXCLUDED.request_count,
        total_response_time = EXCLUDED.total_response_time,
        error_count = EXCLUDED.error_count,
        search_queries = EXCLUDED.search_queries,
        cache_hits = EXCLUDED.cache_hits,
        cache_misses = EXCLUDED.cache_misses,
        api_calls = EXCLUDED.api_calls,
        model_usage = EXCLUDED.model_usage,
        updated_at = CURRENT_TIMESTAMP
"""

LOAD_API_CALLS_SQL = """
    SELECT key, SUM(value::numeric) as total
    FROM metrics, jsonb_each_text(api_calls)
    WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
      AND jsonb_typeof(api_calls) = 'object'
    GROUP BY key
"""

LOAD_MODEL_USAGE_SQL = """
    SELECT key, SUM(value::numeric) as total
    FROM metrics, jsonb_each_text(model_usage)
    WHERE metric_date >= CURRENT_DATE - INTERVAL '30 days'
      AND jsonb_typeof(model_usage) = 'object'
    GROUP BY key
"""


class TestMetricsJsonbRoundtrip:
    """Verify JSONB upsert + jsonb_each_text roundtrip against real Postgres."""

    @pytest.mark.asyncio
    async def test_single_upsert_then_read(self, db_conn):
        """First save, then read — basic happy path."""
        api_calls = {"gemini_streaming": 5}
        model_usage = {"gemini-2.5-flash": 3}

        await db_conn.execute(
            UPSERT_SQL,
            TODAY,
            10,
            1.5,
            0,
            2,
            5,
            1,
            api_calls,
            model_usage,
        )

        rows = await db_conn.fetch(LOAD_API_CALLS_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini_streaming": 5}

        rows = await db_conn.fetch(LOAD_MODEL_USAGE_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini-2.5-flash": 3}

    @pytest.mark.asyncio
    async def test_double_upsert_then_read(self, db_conn):
        """Two saves for the same date (triggers ON CONFLICT), then read.

        This is the exact scenario that caused the jsonb_each_text crash
        when the old code used || to merge JSONB objects.
        """
        api_calls_v1 = {"gemini_streaming": 3}
        model_usage_v1 = {"gemini-2.5-flash": 2}

        await db_conn.execute(
            UPSERT_SQL,
            TODAY,
            5,
            1.0,
            0,
            1,
            3,
            0,
            api_calls_v1,
            model_usage_v1,
        )

        # Second save — same date, updated values
        api_calls_v2 = {"gemini_streaming": 7, "gemini_search": 2}
        model_usage_v2 = {"gemini-2.5-flash": 5}

        await db_conn.execute(
            UPSERT_SQL,
            TODAY,
            12,
            2.5,
            1,
            3,
            8,
            1,
            api_calls_v2,
            model_usage_v2,
        )

        # This would have crashed with the old || merge code
        rows = await db_conn.fetch(LOAD_API_CALLS_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini_streaming": 7, "gemini_search": 2}

        rows = await db_conn.fetch(LOAD_MODEL_USAGE_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini-2.5-flash": 5}

    @pytest.mark.asyncio
    async def test_multiple_dates_aggregation(self, db_conn):
        """Multiple dates aggregate correctly via jsonb_each_text."""
        await db_conn.execute(
            UPSERT_SQL,
            YESTERDAY,
            5,
            1.0,
            0,
            1,
            2,
            0,
            {"gemini_streaming": 3},
            {"gemini-2.5-flash": 2},
        )
        await db_conn.execute(
            UPSERT_SQL,
            TODAY,
            10,
            2.0,
            1,
            2,
            5,
            1,
            {"gemini_streaming": 7, "gemini_search": 1},
            {"gemini-2.5-flash": 5, "gemini-3.1-flash-lite": 2},
        )

        rows = await db_conn.fetch(LOAD_API_CALLS_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini_streaming": 10, "gemini_search": 1}

        rows = await db_conn.fetch(LOAD_MODEL_USAGE_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini-2.5-flash": 7, "gemini-3.1-flash-lite": 2}

    @pytest.mark.asyncio
    async def test_empty_jsonb_object_handled(self, db_conn):
        """Empty JSONB objects should not crash jsonb_each_text."""
        await db_conn.execute(
            UPSERT_SQL,
            TODAY,
            1,
            0.1,
            0,
            0,
            0,
            0,
            {},
            {},
        )

        rows = await db_conn.fetch(LOAD_API_CALLS_SQL)
        assert rows == []

        rows = await db_conn.fetch(LOAD_MODEL_USAGE_SQL)
        assert rows == []

    @pytest.mark.asyncio
    async def test_typeof_guard_skips_corrupted_rows(self, db_conn):
        """If a row somehow has an array, the guard should skip it gracefully."""
        # Insert a valid row
        await db_conn.execute(
            UPSERT_SQL,
            YESTERDAY,
            5,
            1.0,
            0,
            0,
            0,
            0,
            {"gemini_streaming": 3},
            {"gemini-2.5-flash": 2},
        )
        # Manually corrupt another row to be an array
        await db_conn.execute(
            """INSERT INTO metrics (metric_date, api_calls, model_usage)
               VALUES ($1, '[1,2,3]'::jsonb, '"not_an_object"'::jsonb)""",
            YESTERDAY - timedelta(days=1),
        )

        # Should NOT crash — guard skips the corrupted row
        rows = await db_conn.fetch(LOAD_API_CALLS_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini_streaming": 3}

        rows = await db_conn.fetch(LOAD_MODEL_USAGE_SQL)
        result = {row["key"]: int(row["total"]) for row in rows}
        assert result == {"gemini-2.5-flash": 2}

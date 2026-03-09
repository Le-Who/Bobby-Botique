"""Integration tests for user stats — mirrors repos/user_stats.py SQL.

Tests today's request count, weekly stats, and model usage breakdown queries.
"""

import pytest

pytestmark = pytest.mark.integration


class TestUserStatsQueries:
    """Test user metrics queries mirroring repos/user_stats.py."""

    @pytest.mark.asyncio
    async def test_today_request_count(self, db_conn_with_metrics):
        """Mirrors get_user_today_request_count() — COALESCE returns 0 when no row."""
        conn = db_conn_with_metrics
        user_id = 999999

        row = await conn.fetchrow(
            "SELECT COALESCE(request_count, 0) as cnt FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
            user_id,
        )
        assert row["cnt"] == 10

    @pytest.mark.asyncio
    async def test_today_request_count_no_data(self, db_conn_with_user):
        """When no metrics row exists, query should return empty."""
        conn = db_conn_with_user
        user_id = 999999

        rows = await conn.fetch(
            "SELECT COALESCE(request_count, 0) as cnt FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
            user_id,
        )
        assert len(rows) == 0  # No row → caller uses default 0

    @pytest.mark.asyncio
    async def test_weekly_stats_returns_multiple_days(self, db_conn_with_user):
        """Mirrors get_user_weekly_stats() — returns per-day counts for last 7 days."""
        conn = db_conn_with_user
        user_id = 999999

        # Insert metrics for 3 different days
        for offset in (0, 1, 3):
            await conn.execute(
                """INSERT INTO user_metrics (user_id, metric_date, request_count)
                   VALUES ($1, CURRENT_DATE - $2 * INTERVAL '1 day', $3)""",
                user_id,
                offset,
                (offset + 1) * 5,
            )

        rows = await conn.fetch(
            """SELECT metric_date, request_count as cnt
               FROM user_metrics
               WHERE user_id = $1 AND metric_date >= CURRENT_DATE - INTERVAL '6 days'
               ORDER BY metric_date""",
            user_id,
        )
        assert len(rows) == 3
        # Most recent day should be first (ascending order), counts should match
        assert rows[-1]["cnt"] == 5  # Today: offset=0 → count=5

    @pytest.mark.asyncio
    async def test_model_usage_jsonb_breakdown(self, db_conn_with_metrics):
        """Mirrors get_user_model_usage_today() — jsonb_each_text for per-model counts."""
        conn = db_conn_with_metrics
        user_id = 999999

        rows = await conn.fetch(
            """SELECT key as model_name, value::int as cnt
               FROM user_metrics, jsonb_each_text(model_usage)
               WHERE user_id = $1 AND metric_date = CURRENT_DATE
               ORDER BY value::int DESC""",
            user_id,
        )
        assert len(rows) == 2
        assert rows[0]["model_name"] == "gemini-2.5-flash"
        assert rows[0]["cnt"] == 7
        assert rows[1]["model_name"] == "gemini-2.0-flash"
        assert rows[1]["cnt"] == 3

    @pytest.mark.asyncio
    async def test_increment_request_count(self, db_conn_with_metrics):
        """Test incrementing request_count via direct UPDATE (metrics middleware pattern)."""
        conn = db_conn_with_metrics
        user_id = 999999

        await conn.execute(
            """UPDATE user_metrics
               SET request_count = request_count + 1
               WHERE user_id = $1 AND metric_date = CURRENT_DATE""",
            user_id,
        )

        row = await conn.fetchrow(
            "SELECT request_count FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
            user_id,
        )
        assert row["request_count"] == 11  # Was 10, +1 = 11

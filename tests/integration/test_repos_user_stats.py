"""Integration tests for user statistics through the real repository APIs."""

from datetime import date, timedelta

import pytest

from app import metrics
from app.repos import user_stats

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


class TestUserStatsQueries:
    async def test_today_request_count(self, db_conn_with_metrics, test_user_id):
        assert await user_stats.get_user_today_request_count(test_user_id) == 10

    async def test_today_request_count_no_data(self, db_conn_with_user, test_user_id):
        assert await user_stats.get_user_today_request_count(test_user_id) == 0
        assert await user_stats.get_user_model_usage_today(test_user_id) == []

    async def test_weekly_stats_returns_only_last_seven_days(self, db_conn_with_user, test_user_id):
        today = await db_conn_with_user.fetchval("SELECT CURRENT_DATE")
        for offset in (0, 1, 3, 6, 7):
            await db_conn_with_user.execute(
                "INSERT INTO user_metrics (user_id, metric_date, request_count) VALUES ($1, $2, $3)",
                test_user_id,
                today - timedelta(days=offset),
                (offset + 1) * 5,
            )

        rows = await user_stats.get_user_weekly_stats(test_user_id)

        assert rows == [
            {"metric_date": today - timedelta(days=6), "cnt": 35},
            {"metric_date": today - timedelta(days=3), "cnt": 20},
            {"metric_date": today - timedelta(days=1), "cnt": 10},
            {"metric_date": today, "cnt": 5},
        ]

    async def test_model_usage_jsonb_breakdown(self, db_conn_with_metrics, test_user_id):
        assert await user_stats.get_user_model_usage_today(test_user_id) == [
            {"model_name": "gemini-2.5-flash", "cnt": 7},
            {"model_name": "gemini-3.1-flash-lite", "cnt": 3},
        ]

    async def test_request_count_increments_through_metrics_collector(
        self, db_conn_with_metrics, test_user_id, monkeypatch
    ):
        today = await db_conn_with_metrics.fetchval("SELECT CURRENT_DATE")

        class DatabaseDate(date):
            @classmethod
            def today(cls):
                return today

        monkeypatch.setattr(metrics, "date", DatabaseDate)
        collector = metrics.MetricsCollector()
        await collector.record_request("chat", response_time=0.25, user_id=test_user_id)
        # Process the owned queue synchronously without starting a background worker.
        event = collector._events_queue.get_nowait()
        collector._process_event(event)
        collector._events_queue.task_done()
        await collector._save_metrics_to_db()

        assert await user_stats.get_user_today_request_count(test_user_id) == 11
        assert (
            await db_conn_with_metrics.fetchval(
                "SELECT request_count FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
                test_user_id,
            )
            == 11
        )
        assert collector._events_queue.empty()
        assert collector._user_daily[today.isoformat()][test_user_id]["request_count"] == 0

    async def test_other_user_stats_are_not_returned(self, db_conn_with_metrics, test_user_id):
        other_user_id = 888888
        await db_conn_with_metrics.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
        await db_conn_with_metrics.execute(
            "INSERT INTO user_metrics (user_id, metric_date, request_count, model_usage) VALUES ($1, CURRENT_DATE, 99, $2)",
            other_user_id,
            {"other-model": 99},
        )

        assert await user_stats.get_user_today_request_count(test_user_id) == 10
        assert await user_stats.get_user_model_usage_today(test_user_id) == [
            {"model_name": "gemini-2.5-flash", "cnt": 7},
            {"model_name": "gemini-3.1-flash-lite", "cnt": 3},
        ]
        assert await user_stats.get_user_weekly_stats(test_user_id) == [
            {"metric_date": await db_conn_with_metrics.fetchval("SELECT CURRENT_DATE"), "cnt": 10}
        ]

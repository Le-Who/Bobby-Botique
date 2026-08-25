"""Tests for the metrics snapshot race condition fix.

Validates that:
1. Events arriving during DB I/O are NOT lost (atomic snapshot+reset).
2. DB failure triggers compensating re-add of snapshot values.
3. Per-user metrics are also atomically snapshotted.
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def metrics_collector():
    """Create a fresh MetricsCollector for testing."""
    from app.metrics import MetricsCollector

    collector = MetricsCollector()
    # Don't start the background event processor
    collector.running = False
    return collector


class TestMetricsSnapshotAtomicity:
    """Test that snapshot-then-reset is atomic under the lock."""

    @pytest.mark.asyncio
    async def test_events_during_db_write_are_preserved(self, metrics_collector):
        """Core race condition test: events arriving during DB I/O must survive."""
        today = date.today().isoformat()

        # Pre-populate: 5 requests exist before save
        metrics_collector.daily_metrics[today].request_count = 5
        metrics_collector.daily_metrics[today].total_response_time = 2.5
        metrics_collector.daily_metrics[today].cache_hits = 3

        # Simulate: during DB write, 3 more events arrive
        db_write_started = asyncio.Event()
        db_write_done = asyncio.Event()

        original_db_query = AsyncMock()

        async def slow_db_query(*args, **kwargs):
            db_write_started.set()
            await db_write_done.wait()  # Block until we signal completion
            return await original_db_query(*args, **kwargs)

        with patch("app.metrics.db.db_query", side_effect=slow_db_query):
            with patch("app.metrics.db.db_execute_many", new_callable=AsyncMock):
                # Start the save in background
                save_task = asyncio.create_task(metrics_collector._save_metrics_to_db())

                # Wait for DB write to start
                await asyncio.wait_for(db_write_started.wait(), timeout=2.0)

                # Inject events that arrive DURING the DB write
                # These go directly to the daily_metrics (simulating _process_event)
                metrics_collector.daily_metrics[today].request_count += 3
                metrics_collector.daily_metrics[today].total_response_time += 1.0
                metrics_collector.daily_metrics[today].cache_hits += 2

                # Release the DB write
                db_write_done.set()
                await asyncio.wait_for(save_task, timeout=2.0)

        # The 3 events that arrived during DB write must be preserved
        daily = metrics_collector.daily_metrics[today]
        assert daily.request_count == 3, f"Expected 3 (events during save), got {daily.request_count}"
        assert daily.total_response_time == 1.0
        assert daily.cache_hits == 2

    @pytest.mark.asyncio
    async def test_db_failure_compensates(self, metrics_collector):
        """If DB write fails, snapshot values must be re-added to live counters."""
        today = date.today().isoformat()

        metrics_collector.daily_metrics[today].request_count = 10
        metrics_collector.daily_metrics[today].error_count = 2
        metrics_collector.daily_metrics[today].cache_misses = 5
        metrics_collector.daily_metrics[today].api_calls["gemini"] = 7
        metrics_collector.daily_metrics[today].model_usage["flash"] = 4

        with patch("app.metrics.db.db_query", side_effect=Exception("DB down")):
            await metrics_collector._save_metrics_to_db()

        # After failure: counters must be back to original values
        daily = metrics_collector.daily_metrics[today]
        assert daily.request_count == 10, f"Expected 10 (compensated), got {daily.request_count}"
        assert daily.error_count == 2
        assert daily.cache_misses == 5
        assert daily.api_calls.get("gemini") == 7
        assert daily.model_usage.get("flash") == 4

    @pytest.mark.asyncio
    async def test_per_user_metrics_atomically_snapshotted(self, metrics_collector):
        """Per-user metrics must be snapshotted and reset together with daily metrics."""
        today = date.today().isoformat()

        # Set up per-user data
        metrics_collector._user_daily[today][42]["request_count"] = 8
        metrics_collector._user_daily[today][42]["model_usage"]["gemini-flash"] = 6
        metrics_collector.daily_metrics[today].request_count = 8

        with (
            patch("app.metrics.db.db_query", new_callable=AsyncMock),
            patch("app.metrics.db.db_execute_many", new_callable=AsyncMock) as execute_many,
            patch("app.repos.analytics.record_daily_activity", new_callable=AsyncMock),
        ):
            await metrics_collector._save_metrics_to_db()

        # Per-user counters must be reset after save
        user_data = metrics_collector._user_daily[today][42]
        assert user_data["request_count"] == 0, f"Expected 0 (reset after save), got {user_data['request_count']}"
        assert user_data["model_usage"] == {}
        assert execute_many.await_count == 1
        per_user_sql = execute_many.await_args.args[0]
        assert "INSERT INTO public.users" not in per_user_sql
        assert "FROM public.users AS app_user" in per_user_sql
        assert "WHERE app_user.user_id = $1" in per_user_sql

    @pytest.mark.asyncio
    async def test_queued_metrics_never_recreate_erased_user(self, metrics_collector):
        """A pre-erasure snapshot may flush later but must not recreate users."""
        today = date.today().isoformat()
        metrics_collector._user_daily[today][404]["request_count"] = 1
        metrics_collector.daily_metrics[today].request_count = 1

        with (
            patch("app.metrics.db.db_query", new_callable=AsyncMock),
            patch("app.metrics.db.db_execute_many", new_callable=AsyncMock) as execute_many,
            patch("app.repos.analytics.record_daily_activity", new_callable=AsyncMock),
        ):
            await metrics_collector._save_metrics_to_db()

        all_sql = "\n".join(call.args[0] for call in execute_many.await_args_list)
        assert "INSERT INTO public.users" not in all_sql
        assert "INSERT INTO user_metrics" in all_sql
        assert "FROM public.users AS app_user" in all_sql

    @pytest.mark.asyncio
    async def test_per_user_db_failure_compensates(self, metrics_collector):
        """Per-user metrics must be restored on DB write failure."""
        today = date.today().isoformat()

        metrics_collector._user_daily[today][99]["request_count"] = 5
        metrics_collector._user_daily[today][99]["model_usage"]["flash"] = 3
        metrics_collector.daily_metrics[today].request_count = 5

        # First DB call (global metrics) succeeds, but db_execute_many (user metrics) fails
        call_count = 0

        async def selective_fail(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # db_query succeeds
            raise Exception("User metrics write failed")

        with (
            patch("app.metrics.db.db_query", side_effect=selective_fail),
            patch("app.metrics.db.db_execute_many", side_effect=Exception("Batch insert failed")),
        ):
            await metrics_collector._save_metrics_to_db()

        # Per-user data must be compensated
        user_data = metrics_collector._user_daily[today][99]
        assert user_data["request_count"] == 5
        assert user_data["model_usage"].get("flash") == 3

    @pytest.mark.asyncio
    async def test_zero_requests_skips_db_write(self, metrics_collector):
        """If no requests accumulated, DB write should be skipped entirely."""
        today = date.today().isoformat()
        metrics_collector.daily_metrics[today].request_count = 0

        with patch("app.metrics.db.db_query", new_callable=AsyncMock) as mock_query:
            with patch("app.metrics.db.db_execute_many", new_callable=AsyncMock):
                await metrics_collector._save_metrics_to_db()

        # db_query should not have been called for metrics insert
        mock_query.assert_not_called()

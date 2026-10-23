"""Calendar traffic uses user ingress counts and independently measured attempts."""

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import metrics
from app.metrics import MetricsCollector


@pytest.mark.asyncio
async def test_epoch_failure_keeps_consumer_alive_and_retries_on_flush(monkeypatch):
    connected(monkeypatch)
    collector = MetricsCollector()
    query = AsyncMock(side_effect=[RuntimeError("synthetic unavailable"), []])
    monkeypatch.setattr(metrics.db, "db_query", query)
    monkeypatch.setattr(collector, "_load_metrics_from_db", AsyncMock())
    monkeypatch.setattr(collector, "_save_metrics_day", AsyncMock())
    try:
        await collector.initialize()
        assert collector._bg_save_task is not None
        await collector.record_request("handle_message", 1, user_id=42)
        await asyncio.wait_for(collector._events_queue.join(), timeout=2)
        assert collector.metrics.request_count == 1
        await collector._save_metrics_to_db()
        assert query.await_count == 2
        assert all("INSERT INTO traffic_accounting_epoch" in call.args[0] for call in query.await_args_list)
    finally:
        await collector.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table,column",
    [("traffic_daily", "api_attempts"), ("traffic_daily", "source_id"), ("traffic_accounting_epoch", "started_at")],
)
async def test_schema_rejects_missing_traffic_columns(table, column):
    from app.db.schema import EXPECTED_COLUMNS, EXPECTED_TABLES, SchemaValidationError, validate_schema

    query = AsyncMock(
        side_effect=[
            [{"tablename": name} for name in EXPECTED_TABLES],
            [
                {"table_name": name, "column_name": field}
                for name, fields in EXPECTED_COLUMNS.items()
                for field in fields
                if (name, field) != (table, column)
            ],
        ]
    )
    with pytest.raises(SchemaValidationError, match=column):
        await validate_schema(query)


def connected(monkeypatch):
    monkeypatch.setattr(metrics.db, "db_manager", SimpleNamespace(is_connected=True))


@pytest.mark.asyncio
async def test_queued_events_keep_their_kyiv_day_across_midnight(monkeypatch):
    instant = datetime(2026, 7, 1, 20, 59, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(metrics, "datetime", Clock)
    collector = MetricsCollector()
    await collector.record_request("handle_message", 1, user_id=42)
    await collector.record_model_response("winner", user_id=42)
    await collector.record_api_call("gemini", "winner")
    instant = datetime(2026, 7, 1, 21, 1, tzinfo=UTC)
    collector._drain_events()
    assert collector._user_daily["2026-07-01"][42] == {"request_count": 1, "model_usage": {"winner": 1}}
    assert "2026-07-02" not in collector._user_daily
    assert collector._api_daily == {"2026-07-01": 1}


@pytest.mark.asyncio
async def test_api_high_water_write_can_retry_after_an_uncertain_commit(monkeypatch):
    collector = MetricsCollector()
    collector._api_daily["2026-10-23"] = 3
    stored = {}
    uncertain = True

    async def query(sql, params):
        nonlocal uncertain
        day, source, count = params
        stored[day, source] = max(stored.get((day, source), 0), count)
        if uncertain:
            uncertain = False
            raise ConnectionError("synthetic lost acknowledgement")

    monkeypatch.setattr(metrics.db, "db_query", query)
    await collector._save_traffic_to_db()
    assert collector._api_flushed["2026-10-23"] == 0
    await collector._save_traffic_to_db()
    await collector._save_traffic_to_db()
    assert sum(stored.values()) == 3
    collector._api_daily["2026-10-23"] += 2
    await collector._save_traffic_to_db()
    assert sum(stored.values()) == 5
    restarted = MetricsCollector()
    restarted._api_daily["2026-10-23"] = 1
    await restarted._save_traffic_to_db()
    assert sum(stored.values()) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instant,start,end",
    [
        (datetime(2026, 7, 1, 21, 30, tzinfo=UTC), "2026-07-02T00:00:00+03:00", "2026-07-02T00:30:00+03:00"),
        (datetime(2026, 10, 25, 12, tzinfo=UTC), "2026-10-25T00:00:00+03:00", "2026-10-25T14:00:00+02:00"),
    ],
)
async def test_kyiv_midnight_and_dst_period_boundaries(monkeypatch, instant, start, end):
    connected(monkeypatch)
    collector = MetricsCollector()
    day = instant.astimezone(metrics.time_utils.get_kyiv_tz()).date()
    monkeypatch.setattr(
        metrics.db,
        "db_query",
        AsyncMock(
            return_value=[
                {
                    "metric_date": day,
                    "user_requests": 1,
                    "api_attempts": 2,
                    "coverage_start": datetime(2026, 1, 1, tzinfo=UTC),
                }
            ]
        ),
    )
    traffic = await collector.get_traffic_summary(now=instant)
    assert traffic["periods"]["today"]["start"] == start
    assert traffic["periods"]["today"]["end"] == end
    assert traffic["periods"]["today"]["user_requests"] == 1


@pytest.mark.asyncio
async def test_traffic_uses_calendar_days_and_never_mixed_legacy_totals(monkeypatch):
    connected(monkeypatch)
    collector = MetricsCollector()
    collector.metrics.request_count = 999999
    collector.metrics.api_calls = {"document_processing": 999}
    rows = [
        {
            "metric_date": date(2026, 10, 17),
            "user_requests": 4,
            "api_attempts": 2,
            "coverage_start": datetime(2026, 10, 20, 10, tzinfo=UTC),
        },
        {
            "metric_date": date(2026, 10, 23),
            "user_requests": 3,
            "api_attempts": 5,
            "coverage_start": datetime(2026, 10, 20, 10, tzinfo=UTC),
        },
    ]
    monkeypatch.setattr(metrics.db, "db_query", AsyncMock(return_value=rows))
    collector._user_daily["2026-10-23"][42]["request_count"] = 2
    collector._api_daily["2026-10-23"] = 1

    traffic = await collector.get_traffic_summary(now=datetime(2026, 10, 23, 12, tzinfo=UTC))

    assert traffic["timezone"] == "Europe/Kyiv"
    today = traffic["periods"]["today"]
    assert today["start"] == "2026-10-23T00:00:00+03:00"
    assert today["end"] == "2026-10-23T15:00:00+03:00"
    assert (today["user_requests"], today["api_attempts"]) == (5, 6)
    assert today["available"] is True and today["complete"] is True
    week = traffic["periods"]["last_7_days"]
    assert week["start"] == "2026-10-17T00:00:00+03:00"
    assert (week["user_requests"], week["api_attempts"]) == (9, 8)
    assert week["coverage"] == "partial"
    assert week["observed_start"] == "2026-10-20T13:00:00+03:00"
    assert traffic["periods"]["last_30_days"]["start"] == "2026-09-24T00:00:00+03:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("database_error", [False, True])
async def test_traffic_missing_database_never_reports_zero_or_legacy_totals(monkeypatch, database_error):
    monkeypatch.setattr(metrics.db, "db_manager", SimpleNamespace(is_connected=database_error))
    monkeypatch.setattr(metrics.db, "db_query", AsyncMock(side_effect=RuntimeError("synthetic offline")))
    collector = MetricsCollector()
    collector.metrics.request_count = 123
    traffic = await collector.get_traffic_summary(now=datetime(2026, 10, 23, 12, tzinfo=UTC))
    for period in traffic["periods"].values():
        assert period["available"] is False
        assert period["user_requests"] is None
        assert period["api_attempts"] is None


@pytest.mark.asyncio
async def test_traffic_read_waits_for_flush_then_adds_only_unsaved_user_deltas(monkeypatch):
    connected(monkeypatch)
    collector = MetricsCollector()
    now = datetime.now(metrics.time_utils.get_kyiv_tz())
    day = now.date().isoformat()
    collector._user_daily[day][42]["request_count"] = 2
    collector.daily_metrics[day].request_count = 2
    started, release = asyncio.Event(), asyncio.Event()
    persisted_users = 10

    async def query(sql, params=None):
        if "INSERT INTO metrics" in sql:
            started.set()
            await release.wait()
            return []
        return [
            {
                "metric_date": now.date(),
                "user_requests": persisted_users,
                "api_attempts": 0,
                "coverage_start": now.replace(hour=0, minute=0, second=0, microsecond=0),
            }
        ]

    async def users(sql, params):
        nonlocal persisted_users
        persisted_users += sum(row[2] for row in params)

    monkeypatch.setattr(metrics.db, "db_query", query)
    monkeypatch.setattr(metrics.db, "db_execute_many", users)
    monkeypatch.setattr("app.repos.analytics.record_daily_activity", AsyncMock())
    save = asyncio.create_task(collector._save_metrics_to_db())
    await started.wait()
    read = asyncio.create_task(collector.get_traffic_summary(now=now))
    await asyncio.sleep(0)
    assert not read.done()
    collector._user_daily[day][42]["request_count"] += 1
    release.set()
    await save
    traffic = await read
    assert traffic["periods"]["today"]["user_requests"] == 13

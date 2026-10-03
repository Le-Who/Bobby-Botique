"""Round-trip the production metrics writer/reader against migrated PostgreSQL."""

from datetime import date, timedelta

import pytest

from app.metrics import MetricsCollector, PerformanceMetrics

pytestmark = pytest.mark.integration


async def _save_snapshot(collector, *, count, api_calls, model_usage):
    collector.daily_metrics[date.today().isoformat()] = PerformanceMetrics(
        request_count=count,
        total_response_time=1.5,
        api_calls=api_calls,
        model_usage=model_usage,
    )
    await collector._save_metrics_to_db()


async def _load():
    collector = MetricsCollector()
    await collector._load_metrics_from_db()
    return collector.metrics


async def test_single_snapshot_round_trip(db_conn):
    collector = MetricsCollector()
    await _save_snapshot(
        collector,
        count=10,
        api_calls={"gemini_streaming": 5},
        model_usage={"gemini-2.5-flash": 3},
    )
    loaded = await _load()
    assert loaded.request_count == 10
    assert loaded.total_response_time == pytest.approx(1.5)
    assert loaded.api_calls == {"gemini_streaming": 5}
    assert loaded.model_usage == {"gemini-2.5-flash": 3}
    row = await db_conn.fetchrow("SELECT api_calls, model_usage FROM metrics WHERE metric_date = CURRENT_DATE")
    assert isinstance(row["api_calls"], dict)
    assert isinstance(row["model_usage"], dict)
    assert collector.daily_metrics[date.today().isoformat()].request_count == 0


async def test_repeated_delta_saves_accumulate_jsonb_counters(db_conn):
    collector = MetricsCollector()
    await _save_snapshot(
        collector,
        count=5,
        api_calls={"gemini_streaming": 3},
        model_usage={"gemini-2.5-flash": 2},
    )
    await _save_snapshot(
        collector,
        count=7,
        api_calls={"gemini_streaming": 4, "gemini_search": 2},
        model_usage={"gemini-2.5-flash": 3},
    )
    loaded = await _load()
    assert loaded.request_count == 12
    assert loaded.total_response_time == pytest.approx(3.0)
    assert loaded.api_calls == {"gemini_streaming": 7, "gemini_search": 2}
    assert loaded.model_usage == {"gemini-2.5-flash": 5}


async def test_repeated_user_delta_saves_accumulate_model_usage(db_conn_with_user):
    collector = MetricsCollector()
    today = date.today().isoformat()
    for count in (2, 3):
        collector._user_daily[today][999999] = {
            "request_count": count,
            "model_usage": {"gemini-2.5-flash": count},
        }
        await _save_snapshot(
            collector,
            count=count,
            api_calls={"gemini_streaming": count},
            model_usage={"gemini-2.5-flash": count},
        )
    row = await db_conn_with_user.fetchrow(
        "SELECT request_count, model_usage FROM user_metrics WHERE user_id = $1 AND metric_date = CURRENT_DATE",
        999999,
    )
    assert row["request_count"] == 5
    assert row["model_usage"] == {"gemini-2.5-flash": 5}


async def test_reader_aggregates_dates_and_ignores_old_rows(db_conn):
    today = await db_conn.fetchval("SELECT CURRENT_DATE")
    await db_conn.executemany(
        "INSERT INTO metrics (metric_date, request_count, api_calls, model_usage) VALUES ($1, $2, $3, $4)",
        [
            (today - timedelta(days=1), 5, {"gemini_streaming": 3}, {"gemini-2.5-flash": 2}),
            (today, 10, {"gemini_streaming": 7, "gemini_search": 1}, {"gemini-2.5-flash": 5}),
            (today - timedelta(days=31), 100, {"obsolete": 100}, {"obsolete": 100}),
        ],
    )
    loaded = await _load()
    assert loaded.request_count == 15
    assert loaded.api_calls == {"gemini_streaming": 10, "gemini_search": 1}
    assert loaded.model_usage == {"gemini-2.5-flash": 7}


async def test_empty_jsonb_objects_round_trip(db_conn):
    await _save_snapshot(MetricsCollector(), count=1, api_calls={}, model_usage={})
    loaded = await _load()
    assert loaded.request_count == 1
    assert loaded.api_calls == {}
    assert loaded.model_usage == {}


async def test_reader_skips_legacy_non_object_jsonb(db_conn):
    today = await db_conn.fetchval("SELECT CURRENT_DATE")
    await db_conn.executemany(
        "INSERT INTO metrics (metric_date, request_count, api_calls, model_usage) VALUES ($1, $2, $3, $4)",
        [
            (today - timedelta(days=1), 5, {"gemini_streaming": 3}, {"gemini-2.5-flash": 2}),
            (today - timedelta(days=2), 1, [1, 2, 3], "not_an_object"),
        ],
    )
    loaded = await _load()
    assert loaded.api_calls == {"gemini_streaming": 3}
    assert loaded.model_usage == {"gemini-2.5-flash": 2}

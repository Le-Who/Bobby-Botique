"""Real telemetry readers must retain independent last-good dashboard evidence."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import cache, metrics, web
from app.dashboard_sources import BotLifecycle, DashboardSourceCache
from app.metrics import MetricsCollector
from app.queue import task_queue
from app.utils import time as time_utils


@pytest.fixture
async def telemetry(monkeypatch):
    clock = [0.0]
    state = {"sql_failed": False, "redis_failed": False, "sql_calls": 0, "redis_calls": 0}
    collector = MetricsCollector()
    collector.metrics.request_count = 12
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    monkeypatch.setattr(metrics.db, "db_manager", SimpleNamespace(is_connected=True))

    async def query(sql, params):
        assert "FROM generate_series" in sql
        state["sql_calls"] += 1
        if state["sql_failed"]:
            raise ConnectionError("synthetic SQL failure; no detail belongs in output")
        return [
            {
                "metric_date": datetime.now(time_utils.get_kyiv_tz()).date(),
                "coverage_start": datetime.now(UTC),
                "user_requests": 8,
                "api_attempts": 11,
                "own_api_attempts": 0,
            }
        ]

    async def redis_info():
        state["redis_calls"] += 1
        if state.get("redis_hung"):
            await asyncio.Event().wait()
        if state["redis_failed"]:
            raise RedisConnectionError("synthetic Redis failure; no detail belongs in output")
        return {"db0": {"keys": 9}, "used_memory_human": "2M"}

    async def single_attempt(operation, *args, **kwargs):
        return await operation(*args, **kwargs)

    monkeypatch.setattr(metrics.db, "db_query", query)
    monkeypatch.setattr(
        cache,
        "redis_client",
        SimpleNamespace(info=redis_info, connection_pool=SimpleNamespace(connection_kwargs={"db": 0})),
    )
    monkeypatch.setattr(cache, "_redis_operation_with_retry", single_attempt)
    monkeypatch.setattr(cache, "multi_layer_cache", cache.MultiLayerCache())
    monkeypatch.setattr(
        web,
        "DashboardSourceCache",
        lambda sources, **kwargs: DashboardSourceCache(
            sources, clock=lambda: clock[0], wall_clock=lambda: 100 + clock[0], **kwargs
        ),
    )
    monkeypatch.setattr(web, "_sample_system_metrics", lambda: {"cpu_percent": 22})
    for owner, name, value in (
        (metrics.role_conv_metrics, "get_metrics_summary", {}),
        (cache, "ping_safe", True),
        (web, "get_supabase_metrics", {"status": "connected"}),
        (web, "get_gemini_key_usage_stats", []),
        (web, "get_tavily_key_usage_stats", []),
        (web, "_get_dashboard_key_health", {}),
        (task_queue, "get_queue_stats", {}),
    ):
        monkeypatch.setattr(owner, name, AsyncMock(return_value=value))
    try:
        yield clock, state, collector
    finally:
        await web._close_dashboard_sources()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle_state", ["starting", "ready", "draining", "stopped", "failed"])
@pytest.mark.parametrize("path", ["/api/dashboard", "/api/overview"])
async def test_http_service_reports_actual_bot_lifecycle(monkeypatch, telemetry, lifecycle_state, path):
    lifecycle = BotLifecycle()
    lifecycle.set(lifecycle_state)
    monkeypatch.setattr(web, "bot_lifecycle", lifecycle)
    monkeypatch.setattr(web.database, "is_database_connected", lambda: True)
    client = web.quart_app.test_client()
    async with client.session_transaction() as admin_session:
        admin_session["authenticated"] = True
    response = await client.get(path)
    assert response.status_code == 200
    payload = await response.get_json()
    overview = payload["overview"] if path == "/api/dashboard" else payload
    assert overview["services"]["bot"] == lifecycle_state


@pytest.mark.asyncio
async def test_caught_sql_failure_keeps_traffic_age_and_process_counters_independent(telemetry):
    clock, state, collector = telemetry
    good = await web._build_dashboard_snapshot()
    assert good["overview"]["metrics"]["traffic"]["periods"]["today"]["user_requests"] == 8
    state["sql_failed"] = True
    collector.metrics.request_count = 13
    clock[0] = 16
    stale = await web._build_dashboard_snapshot()
    source = stale["sources"]["traffic"]
    assert source["available"] and source["stale"]
    assert source["observed_at"] == good["sources"]["traffic"]["observed_at"] == 100
    assert source["reason_code"] == "source_unavailable"
    assert source["scope"] == "mixed"  # Persisted database rows plus this process's pending deltas.
    period = stale["overview"]["metrics"]["traffic"]["periods"]["today"]
    assert period["user_requests"] == 8
    assert period["complete"] is False and period["coverage"] == "partial"
    assert stale["overview"]["metrics"]["total_requests"] == 13
    assert stale["sources"]["metrics"]["stale"] is False
    assert stale["sources"]["metrics"]["scope"] == "process"
    clock[0] = 61
    unavailable = await web._build_dashboard_snapshot()
    assert unavailable["sources"]["traffic"]["available"] is False
    assert unavailable["sources"]["traffic"]["observed_at"] == 100
    assert unavailable["overview"]["metrics"]["traffic"] is None
    assert state["sql_calls"] == 3
    assert "synthetic SQL failure" not in repr(unavailable)
    event = await web._build_dashboard_event()
    assert event["metrics"]["traffic"] is None
    assert event["sources"]["traffic"]["available"] is False
    assert event["sources"]["traffic"]["observed_at"] == 100
    assert state["sql_calls"] == 3


@pytest.mark.asyncio
async def test_nested_redis_failure_keeps_redis_last_good_and_current_memory(telemetry):
    clock, state, _ = telemetry
    good = await web._build_dashboard_snapshot()
    assert good["infrastructure"]["cache"]["redis_keys"] == 9
    state["redis_failed"] = True
    cache.multi_layer_cache.default_cache["synthetic"] = {"synthetic": True}
    clock[0] = 16
    stale = await web._build_dashboard_snapshot()
    redis_source = stale["sources"]["redis_cache"]
    assert redis_source["available"] and redis_source["stale"]
    assert redis_source["observed_at"] == good["sources"]["redis_cache"]["observed_at"] == 100
    assert redis_source["scope"] == "redis"
    assert stale["infrastructure"]["cache"]["redis_keys"] == 9
    assert stale["infrastructure"]["cache"]["memory_items"] == 1
    assert stale["sources"]["memory_cache"]["available"] is True
    assert stale["sources"]["memory_cache"]["stale"] is False
    assert stale["sources"]["memory_cache"]["observed_at"] == 116
    assert stale["sources"]["memory_cache"]["scope"] == "process"
    clock[0] = 61
    unavailable = await web._build_dashboard_snapshot()
    current_cache = unavailable["infrastructure"]["cache"]
    assert current_cache["available"] and current_cache["memory_available"]
    assert current_cache["redis_available"] is False
    assert current_cache["redis_keys"] is None
    assert current_cache["memory_items"] == 1
    assert current_cache["redis_source"]["observed_at"] == 100
    assert state["redis_calls"] == 3
    assert "synthetic Redis failure" not in repr(unavailable)


@pytest.mark.asyncio
async def test_twenty_actual_snapshot_sse_readers_share_one_sql_and_redis_read(telemetry):
    _, state, _ = telemetry
    results = await asyncio.gather(
        *[web._build_dashboard_snapshot() if i % 2 else web._build_dashboard_event() for i in range(20)]
    )
    assert all(row["sources"]["traffic"]["available"] for row in results)
    assert state["sql_calls"] == 1
    assert state["redis_calls"] == 1


@pytest.mark.asyncio
async def test_process_summary_can_skip_traffic_without_changing_legacy_default(telemetry):
    _, state, collector = telemetry
    summary = await collector.get_metrics_summary(include_traffic=False)
    assert summary["total_requests"] == 12
    assert "traffic" not in summary
    assert state["sql_calls"] == 0
    legacy = await collector.get_metrics_summary()
    assert legacy["traffic"]["periods"]["today"]["user_requests"] == 8
    assert state["sql_calls"] == 1


@pytest.mark.asyncio
async def test_partly_available_real_traffic_periods_preserve_their_individual_flags(monkeypatch, telemetry):
    day = datetime.now(time_utils.get_kyiv_tz()).date()
    rows = [
        {
            "metric_date": day,
            "coverage_start": datetime.now(UTC),
            "user_requests": 8,
            "api_attempts": 11,
            "own_api_attempts": 0,
        },
        {
            "metric_date": day - timedelta(days=1),
            "coverage_start": datetime.now(UTC),
            "user_requests": "synthetic-invalid-count",
            "api_attempts": 1,
            "own_api_attempts": 0,
        },
    ]
    monkeypatch.setattr(metrics.db, "db_query", AsyncMock(return_value=rows))
    snapshot = await web._build_dashboard_snapshot()
    assert snapshot["sources"]["traffic"]["available"] is True
    assert snapshot["sources"]["traffic"]["stale"] is False
    periods = snapshot["overview"]["metrics"]["traffic"]["periods"]
    assert periods["today"]["available"] is True
    assert periods["today"]["user_requests"] == 8
    assert periods["last_7_days"]["available"] is False
    assert periods["last_30_days"]["available"] is False
    assert periods["last_7_days"]["user_requests"] is None


@pytest.mark.asyncio
async def test_hung_redis_does_not_hide_available_process_memory(monkeypatch, telemetry):
    _, state, _ = telemetry
    state["redis_hung"] = True
    monkeypatch.setattr(web, "_DASHBOARD_SOURCE_TIMEOUT", 0.01)
    snapshot = await web._build_dashboard_snapshot()
    assert snapshot["sources"]["redis_cache"]["available"] is False
    assert snapshot["sources"]["redis_cache"]["reason_code"] == "source_timeout"
    assert snapshot["sources"]["memory_cache"]["available"] is True
    assert snapshot["infrastructure"]["cache"]["memory_items"] == 0
    assert snapshot["infrastructure"]["cache"]["redis_keys"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_lanes", ["all", "partial"])
async def test_real_queue_caught_lane_failures_have_stale_or_partial_source_state(monkeypatch, telemetry, failed_lanes):
    from app import queue as queue_module
    from app.queue_runtime import TaskQueue
    from app.queue_store import MemoryQueueStore

    clock, _, _ = telemetry
    store = MemoryQueueStore(clock=lambda: 100 + clock[0])
    queue = TaskQueue(store=store, clock=lambda: 100 + clock[0])
    monkeypatch.setattr(queue_module, "task_queue", queue)
    monkeypatch.setattr(queue_module, "_get_redis", lambda: None)
    for i in range(2):
        await queue.enqueue_job(0, "cleanup_metrics", {}, lane="maintenance", idempotency_key=f"queue-observation-{i}")
    first = await web._build_dashboard_snapshot()
    assert first["infrastructure"]["queue"]["pending"] == 2
    original = store.stats

    async def stats(lane):
        if failed_lanes == "all" or lane == "daily":
            raise ConnectionError("synthetic lane reader failure")
        return await original(lane)

    monkeypatch.setattr(store, "stats", stats)
    clock[0] = 16
    result = await web._build_dashboard_snapshot()
    source = result["sources"]["queue"]
    if failed_lanes == "partial":
        assert source["available"] is True and source["stale"] is False
        assert source["reason_code"] == "source_partial"
        current = result["infrastructure"]["queue"]
        assert current["partial"] is True
        assert current["lanes"]["maintenance"]["queued"] == 2
        assert current["lanes"]["daily"]["available"] is False
        assert current["lanes"]["daily"].get("queued") is None
        assert current["pending"] is None
    else:
        assert source["available"] is True and source["stale"] is True
        assert source["observed_at"] == 100
        assert result["infrastructure"]["queue"]["pending"] == 2
        clock[0] = 61
        unavailable = await web._build_dashboard_snapshot()
        assert unavailable["sources"]["queue"]["available"] is False
        assert unavailable["sources"]["queue"]["observed_at"] == 100
        assert unavailable["infrastructure"]["queue"]["pending"] is None

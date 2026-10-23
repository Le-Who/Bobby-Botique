"""API attempts and user-visible model responses have separate metric owners."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.metrics import MetricsCollector
from app.utils import time as time_utils


@pytest.mark.asyncio
async def test_handled_failure_reaches_ingress_once_and_is_consumed():
    from app.utils.metrics_middleware import mark_operation_failed

    collector = MetricsCollector()
    mark_operation_failed()
    await collector.record_request("internal_stage", 0.1)
    await collector.record_request("handle_message", 1, user_id=42)
    await collector.record_request("handle_message", 1, user_id=42)
    mark_operation_failed()
    await collector.record_request("handle_message", 1, success=False, user_id=42)
    await collector.record_request("handle_message", 1, user_id=42)
    collector._drain_events()
    assert collector.metrics.request_count == 4
    assert collector.metrics.error_count == 2


def drain_events(collector):
    while not collector._events_queue.empty():
        collector._process_event(collector._events_queue.get_nowait())
        collector._events_queue.task_done()


@pytest.mark.asyncio
async def test_internal_stages_do_not_count_as_user_operations():
    collector = MetricsCollector()
    await collector.record_request("chat", 1.0)
    await collector.record_request("photo", 0.5)
    await collector.record_request("handle_message", 2.0, success=False, user_id=42)
    drain_events(collector)
    assert collector.metrics.request_count == 1
    assert collector.metrics.error_count == 1
    assert collector._user_daily[datetime.now(time_utils.get_kyiv_tz()).date().isoformat()][42]["request_count"] == 1


@pytest.mark.asyncio
async def test_direct_workloads_count_each_attempt_including_failed_and_cancelled(monkeypatch):
    import asyncio

    from app import metrics
    from app.observability.workload_events import observe_workload_call

    collector = MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", collector)

    async def result(outcome):
        if outcome == "failed":
            raise ValueError("synthetic")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return "audio"

    for outcome in ("succeeded", "failed", "cancelled"):
        try:
            await observe_workload_call(
                result(outcome), workload="tts", provider="gemini", model="voice-model", api_key="fake-key"
            )
        except ValueError, asyncio.CancelledError:
            pass
    drain_events(collector)
    assert collector.metrics.api_calls == {"gemini": 3}
    assert collector.metrics.model_usage == {"voice-model": 3}
    assert collector.metrics.request_count == 0


@pytest.mark.asyncio
async def test_model_response_does_not_add_an_api_attempt_or_user_request():
    collector = MetricsCollector()
    await collector.record_api_call("gemini", "gemini-failed")
    await collector.record_api_call("gemini", "gemini-winner")
    await collector.record_model_response("gemini-winner", user_id=42)

    drain_events(collector)

    assert collector.metrics.api_calls == {"gemini": 2}
    assert collector.metrics.model_usage == {"gemini-failed": 1, "gemini-winner": 1}
    user_metrics = collector._user_daily[datetime.now(time_utils.get_kyiv_tz()).date().isoformat()][42]
    assert user_metrics == {"request_count": 0, "model_usage": {"gemini-winner": 1}}
    assert len(collector.api_event_log) == 2


@pytest.mark.asyncio
async def test_model_response_without_an_actor_does_not_invent_user_metrics():
    collector = MetricsCollector()

    await collector.record_model_response("gemini-test", user_id=None)
    drain_events(collector)

    assert collector.metrics.model_usage == {}
    assert dict(collector._user_daily) == {}


@pytest.mark.asyncio
async def test_user_model_response_flushes_without_global_usage_delta(monkeypatch):
    from app import metrics

    collector = MetricsCollector()
    await collector.record_model_response("gemini-winner", user_id=42)
    drain_events(collector)
    writes = []

    async def write_users(sql, params):
        writes.extend(params)

    global_write = AsyncMock()
    monkeypatch.setattr(metrics.db, "db_query", global_write)
    monkeypatch.setattr(metrics.db, "db_execute_many", write_users)
    monkeypatch.setattr("app.repos.analytics.record_daily_activity", AsyncMock())

    await collector._save_metrics_to_db()

    assert writes == [(42, datetime.now(time_utils.get_kyiv_tz()).date(), 0, {"gemini-winner": 1})]
    assert collector._user_daily[datetime.now(time_utils.get_kyiv_tz()).date().isoformat()][42]["model_usage"] == {}
    global_write.assert_not_awaited()

"""API attempts and user-visible model responses have separate metric owners."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.metrics import MetricsCollector


def drain_events(collector):
    while not collector._events_queue.empty():
        collector._process_event(collector._events_queue.get_nowait())
        collector._events_queue.task_done()


@pytest.mark.asyncio
async def test_model_response_does_not_add_an_api_attempt_or_user_request():
    collector = MetricsCollector()
    await collector.record_api_call("gemini", "gemini-failed")
    await collector.record_api_call("gemini", "gemini-winner")
    await collector.record_model_response("gemini-winner", user_id=42)

    drain_events(collector)

    assert collector.metrics.api_calls == {"gemini": 2}
    assert collector.metrics.model_usage == {"gemini-failed": 1, "gemini-winner": 1}
    user_metrics = collector._user_daily[date.today().isoformat()][42]
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

    assert writes == [(42, date.today(), 0, {"gemini-winner": 1})]
    assert collector._user_daily[date.today().isoformat()][42]["model_usage"] == {}
    global_write.assert_not_awaited()

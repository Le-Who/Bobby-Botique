"""Operational HTTP timestamps must be parseable, timezone-aware UTC values."""

import datetime
from collections import defaultdict, deque
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def client(monkeypatch):
    from app import web

    monkeypatch.setitem(web.quart_app.config, "TESTING", True)
    monkeypatch.setattr(web._api_limiter, "_requests", defaultdict(deque))
    monkeypatch.setattr(web._api_limiter, "_call_count", 0)
    return web.quart_app.test_client()


@pytest.mark.asyncio
@pytest.mark.parametrize("connected, status", [(True, 200), (False, 503)])
async def test_health_timestamp_is_parseable_utc(client, monkeypatch, connected, status):
    from app import cache, web

    monkeypatch.setattr(web.database, "is_database_connected", lambda: connected)
    monkeypatch.setattr(cache, "redis_client", None)

    response = await client.get("/health")

    assert response.status_code == status
    payload = await response.get_json()
    timestamp = datetime.datetime.fromisoformat(payload["timestamp"])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == datetime.timedelta(0)
    assert payload["services"]["database"] == ("connected" if connected else "disconnected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, section, expected",
    [
        ("/api/errors", "error_count", 2),
        ("/api/queue", "queue", {"total_pending": 3, "processing": 1}),
        ("/api/database", "database", {"connected": True}),
    ],
)
async def test_admin_api_timestamp_is_parseable_utc(client, monkeypatch, path, section, expected):
    from app import web
    from app.metrics import metrics_collector
    from app.queue import task_queue

    monkeypatch.setattr(web, "settings", web.settings.model_copy(update={"ADMIN_SECRET": "timestamp-test-secret"}))
    monkeypatch.setattr(metrics_collector, "get_metrics_summary", AsyncMock(return_value={"error_count": 2}))
    monkeypatch.setattr(task_queue, "get_queue_stats", AsyncMock(return_value={"total_pending": 3, "processing": 1}))
    monkeypatch.setattr(web, "get_supabase_metrics", AsyncMock(return_value={"connected": True}))

    response = await client.get(path, headers={"X-Auth-Token": "timestamp-test-secret"})

    assert response.status_code == 200
    payload = await response.get_json()
    timestamp = datetime.datetime.fromisoformat(payload["timestamp"])
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() == datetime.timedelta(0)
    assert payload[section] == expected

from collections import deque
from pathlib import Path

import pytest

from app import web
from app.metrics import MetricsCollector


@pytest.mark.asyncio
async def test_metrics_summary_exposes_canonical_dashboard_fields_newest_error_first():
    collector = MetricsCollector()
    collector.metrics.request_count = 8
    collector.metrics.total_response_time = 2.0
    collector.metrics.error_count = 2
    collector.metrics.cache_hits = 3
    collector.metrics.cache_misses = 1
    collector.response_times.extend([0.1, 0.3])
    collector.error_log = deque(
        [
            {"timestamp": "2026-08-29T10:00:00+00:00", "message": "older"},
            {"timestamp": "2026-08-29T11:00:00+00:00", "message": "newer"},
        ],
        maxlen=100,
    )

    summary = await collector.get_metrics_summary()

    assert summary["total_requests"] == 8
    assert summary["average_response_time_ms"] == 200
    assert summary["error_count"] == 2
    assert summary["error_rate_percent"] == 25
    assert summary["cache_hit_rate_percent"] == 75
    assert [error["message"] for error in summary["recent_errors"]] == ["newer", "older"]


def test_dashboard_snapshot_has_canonical_sections_and_fields():
    raw = {
        "metrics": {
            "total_requests": 12,
            "average_response_time_ms": 125,
            "error_count": 3,
            "error_rate_percent": 25,
            "cache_hit_rate_percent": 80,
            "recent_errors": [{"timestamp": "2026-08-29T11:00:00+00:00", "type": "new", "message": "latest"}],
        },
        "summarization": {"triggered": 4},
        "redis_ping": True,
        "db_metrics": {"status": "connected", "pool_size": 5, "free_size": 3},
        "cache": {
            "redis": {"total_keys": 9, "used_memory": "2M"},
            "memory": {"memory_items": 2, "memory_max_size": 1200, "memory_utilization": 0.2},
        },
        "queue": {"pending_tasks": 1, "running_tasks": 2, "completed_tasks": 3, "failed_tasks": 4, "active_workers": 5},
        "keys_gemini": [{"model_name": "gemini-3.7-flash", "key_hash": "abc", "request_count": 0}],
        "keys_tavily": [],
        "key_health": {"total_keys_tracked": 1, "keys": [{"key": "abc", "status": "active"}]},
    }

    snapshot = web._assemble_dashboard_snapshot(
        raw,
        system={"cpu_percent": 1, "memory_percent": 2, "disk_percent": 3},
        circuit_breakers={},
        generated_at="2026-08-29T12:00:00+00:00",
    )

    assert set(snapshot) == {"overview", "providers", "infrastructure", "errors", "generated_at"}
    assert snapshot["overview"]["metrics"] == {
        "total_requests": 12,
        "average_response_time_ms": 125,
        "error_count": 3,
        "error_rate_percent": 25,
        "cache_hit_rate_percent": 80,
    }
    assert snapshot["providers"]["gemini"]["rows"][0]["request_count"] == 0
    assert snapshot["infrastructure"]["cache"]["memory_items"] == 2
    assert snapshot["infrastructure"]["queue"]["workers"] == 5
    assert snapshot["errors"]["recent"][0]["message"] == "latest"


def test_dashboard_snapshot_survives_uninitialized_settings(monkeypatch):
    monkeypatch.setattr(web, "settings", None)

    snapshot = web._assemble_dashboard_snapshot(
        {
            "metrics": {"error": "metrics unavailable"},
            "keys_gemini": {"error": "keys unavailable"},
            "keys_tavily": {"error": "keys unavailable"},
        },
        system={},
        circuit_breakers={},
    )

    assert snapshot["providers"]["gemini"]["available"] is False
    assert snapshot["providers"]["reset_info"]["tavily_credit_limit"] is None
    assert snapshot["errors"]["available"] is False


def test_dashboard_template_uses_one_snapshot_request_and_escapes_dynamic_errors():
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")

    assert 'dashboard: "/api/dashboard"' in template
    assert 'overview: "/api/overview"' not in template
    assert 'keys: "/api/keys"' not in template
    assert "const d = await apiFetch(API.dashboard)" in template
    assert "escapeHtml" in template
    assert "escapeHtml(msg)" in template
    assert "Metrics unavailable" in template
    assert "Error metrics unavailable" in template

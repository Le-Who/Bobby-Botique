"""Offline regressions for bounded and truthful dashboard source reporting."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import web
from app.repos import keys, metrics_repo


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_database_metrics_report_live_public_pool_counts(monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock())
    pool = SimpleNamespace(
        get_size=lambda: 7,
        get_idle_size=lambda: 3,
        acquire=lambda: _ConnectionContext(connection),
    )
    monkeypatch.setattr(metrics_repo.db_manager, "pool", pool)
    monkeypatch.setattr(metrics_repo.db_manager, "_is_pool_closed", lambda: False)

    result = await metrics_repo.get_supabase_metrics()

    assert result["status"] == "connected"
    assert result["pool_size"] == 7
    assert result["free_size"] == 3
    assert result["active_connections"] == 4


@pytest.mark.asyncio
async def test_database_probe_failure_has_safe_code(monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock(side_effect=asyncpg.InterfaceError("private connection details")))
    pool = SimpleNamespace(
        get_size=lambda: 7,
        get_idle_size=lambda: 3,
        acquire=lambda: _ConnectionContext(connection),
    )
    monkeypatch.setattr(metrics_repo.db_manager, "pool", pool)
    monkeypatch.setattr(metrics_repo.db_manager, "_is_pool_closed", lambda: False)

    result = await metrics_repo.get_supabase_metrics()

    assert result["error"] == "database_unavailable"


@pytest.mark.asyncio
async def test_dashboard_source_deadline_returns_partial_failure_and_drains_work(monkeypatch):
    monkeypatch.setattr(web, "_DASHBOARD_SOURCE_TIMEOUT", 0.01, raising=False)
    stopped = asyncio.Event()

    async def stalled_source():
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    result = await asyncio.wait_for(web._safe_fetch("queue", stalled_source()), timeout=0.2)

    assert result == ("queue", {"error": "source_timeout"})
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_dashboard_source_failure_does_not_expose_dependency_message():
    async def failed_source():
        raise RuntimeError("synthetic private connection details")

    result = await web._safe_fetch("database", failed_source())

    assert result == ("database", {"error": "source_unavailable"})


def test_absent_dashboard_sources_are_unknown_instead_of_successful_empty_data():
    snapshot = web._assemble_dashboard_snapshot({}, system={}, circuit_breakers={})

    assert snapshot["overview"]["available"] is False
    assert snapshot["providers"]["gemini"]["available"] is False
    assert snapshot["providers"]["key_health"]["available"] is False
    assert snapshot["errors"]["available"] is False


def test_failed_database_probe_does_not_fall_back_to_open_pool_health(monkeypatch):
    monkeypatch.setattr(web.database, "is_database_connected", lambda: True)

    snapshot = web._assemble_dashboard_snapshot(
        {"db_metrics": {"status": "error", "error": "database_unavailable"}}, system={}, circuit_breakers={}
    )

    assert snapshot["overview"]["services"]["database"] == "unknown"
    assert snapshot["overview"]["services"]["redis"] == "unknown"


@pytest.mark.asyncio
async def test_key_health_reports_expired_cooldowns_and_bounded_coverage(monkeypatch):
    now = datetime.now(UTC)
    statuses = [
        {
            "key_hash": f"hash-{index:04d}",
            "model_name": "gemini-test",
            "status": "suspended",
            "suspended_until": now + timedelta(hours=1) if index == 0 else now - timedelta(hours=1),
            "failure_count": 1,
            "last_error": "synthetic historical failure",
            "updated_at": now,
        }
        for index in range(22)
    ]
    manager = keys.KeyStatusManager()
    monkeypatch.setattr(manager, "get_all_statuses", AsyncMock(return_value=statuses))

    summary = await manager.get_health_summary()

    assert summary["active"] == 21
    assert summary["suspended"] == 1
    assert summary["scope"] == "key_model_pairs"
    assert summary["truncated"] is True
    assert summary["total_key_model_pairs"] == 22
    assert len(summary["keys"]) == 20
    assert summary["keys"][0]["status"] == "suspended"
    expired = summary["keys"][1]
    assert expired["key_hash"] == "hash-0001"
    assert expired["status"] == "active"
    assert expired["recorded_status"] == "suspended"
    assert expired["suspension_expired"] is True


@pytest.mark.asyncio
async def test_key_health_marks_complete_empty_history(monkeypatch):
    manager = keys.KeyStatusManager()
    monkeypatch.setattr(manager, "get_all_statuses", AsyncMock(return_value=[]))

    summary = await manager.get_health_summary()

    assert summary["truncated"] is False
    assert summary["total_key_model_pairs"] == 0
    assert summary["keys"] == []


@pytest.mark.asyncio
async def test_key_health_scrubs_historical_provider_credentials_before_truncation(monkeypatch):
    credential = "synthetic-provider-key-0123456789abcdef"
    manager = keys.KeyStatusManager()
    monkeypatch.setattr(
        manager,
        "get_all_statuses",
        AsyncMock(
            return_value=[
                {
                    "key_hash": "safe-key-hash",
                    "model_name": "gemini-test",
                    "status": "suspended",
                    "suspended_until": None,
                    "failure_count": 1,
                    "last_error": "x" * 80 + credential + " transport failed",
                }
            ]
        ),
    )

    summary = await manager.get_health_summary()

    error = summary["keys"][0]["last_error"]
    assert "synthetic-provider" not in error
    assert "[redacted]" in error
    assert len(error) <= 100


@pytest.mark.asyncio
async def test_controls_get_resolves_dynamic_baselines_once_for_both_inventories(monkeypatch):
    from types import MappingProxyType

    from app import config, process_policies, web_controls
    from app.runtime_settings import lifecycle, models, prompts, source_inventory, store

    snapshot = store.SettingsSnapshot(7, MappingProxyType({}))
    monkeypatch.setattr(web_controls, "get_state", AsyncMock(return_value=(snapshot, [])))
    monkeypatch.setattr(lifecycle, "load_controlled_prompts", lambda: None)
    monkeypatch.setattr(models, "list_catalogs", AsyncMock(return_value=[]))
    monkeypatch.setattr(prompts, "list_prompts", AsyncMock(return_value=[]))
    monkeypatch.setattr(web_controls.audio, "list_audio_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(process_policies, "PROCESSES", {"inline": process_policies.PROCESSES["inline"]})
    monkeypatch.setattr(source_inventory, "get_source_inventory", lambda: SimpleNamespace(process_consumers={}))
    settings = SimpleNamespace(DAILY_LIMITS={}, AVAILABLE_MODELS=[])
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(keys, "settings", settings)
    monkeypatch.setattr(keys.db_manager, "_model_config_cache", {"gemini-dynamic": 11})
    reads = []

    async def baseline(process_id):
        reads.append(process_id)
        return ("gemini-dynamic",)

    monkeypatch.setattr(process_policies, "baseline_models", baseline)
    client = web.quart_app.test_client()
    async with client.session_transaction() as admin_session:
        admin_session["authenticated"] = True

    response = await client.get("/api/admin/controls")

    assert response.status_code == 200
    data = await response.get_json()
    assert data["revision"] == 7
    assert data["processes"][0]["models"] == ["gemini-dynamic"]
    assert [(row["model"], row["limit"]) for row in data["limits"]] == [("gemini-dynamic", 11)]
    assert reads == ["inline"]

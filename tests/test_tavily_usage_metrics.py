"""Tavily diagnostics must describe the admission configuration actually in use."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repos import keys, metrics_repo


@pytest.mark.asyncio
async def test_tavily_metrics_uses_startup_manager_after_settings_reload(monkeypatch):
    manager = keys.MonthlyKeyManager("tavily_api_keys", "tavily_key_usage", 1000, 0.97)
    monkeypatch.setattr(keys, "_tavily_km", manager)
    monkeypatch.setattr(
        metrics_repo,
        "settings",
        SimpleNamespace(TAVILY_MONTHLY_CREDIT_LIMIT=500, TAVILY_LIMIT_THRESHOLD_PERCENT=0.5),
    )
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(metrics_repo, "db_query", query)

    assert await metrics_repo.get_tavily_key_usage_stats() == []

    parameters = query.await_args.args[1]
    assert parameters[:2] == (1000, 0.97), "A settings reload does not recreate the monthly key manager"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/keys", "/api/dashboard"])
async def test_empty_tavily_headers_use_admission_manager_after_reload(monkeypatch, path):
    from app import web

    monkeypatch.setattr(keys, "_tavily_km", keys.MonthlyKeyManager("tavily_api_keys", "tavily_key_usage", 1000, 0.97))
    monkeypatch.setattr(web, "settings", SimpleNamespace(TAVILY_MONTHLY_CREDIT_LIMIT=500, DAILY_LIMITS={}))
    monkeypatch.setattr(web, "get_gemini_key_usage_stats", AsyncMock(return_value=[]))
    monkeypatch.setattr(web, "get_tavily_key_usage_stats", AsyncMock(return_value=[]))

    async def snapshot():
        return web._assemble_dashboard_snapshot({"keys_tavily": []}, system={}, circuit_breakers={})

    monkeypatch.setattr(web, "_build_dashboard_snapshot", snapshot)
    client = web.quart_app.test_client()
    async with client.session_transaction() as session:
        session["authenticated"] = True

    response = await client.get(path)

    assert response.status_code == 200
    data = await response.get_json()
    section = data["providers"] if path == "/api/dashboard" else data
    assert section["reset_info"]["tavily_credit_limit"] == 1000

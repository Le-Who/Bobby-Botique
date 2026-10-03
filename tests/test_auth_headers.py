from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def client(monkeypatch):
    from app import cache, metrics, web

    monkeypatch.setattr(web, "settings", web.settings.model_copy(update={"ADMIN_SECRET": "test_secret_token"}))
    monkeypatch.setitem(web.quart_app.config, "TESTING", True)
    monkeypatch.setattr(metrics.metrics_collector, "get_metrics_summary", AsyncMock(return_value={}))
    monkeypatch.setattr(cache, "ping_safe", AsyncMock(return_value=False))
    return web.quart_app.test_client()


@pytest.mark.asyncio
async def test_query_param_auth_rejected(client):
    """
    Test that authentication via query parameter is REJECTED (vulnerability fixed).
    """
    # Verify accessing protected API endpoint with query param (not header)
    response = await client.get("/api/overview?token=test_secret_token")

    # DESIRED BEHAVIOR: 401 Unauthorized (query params ignored for auth)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_header_auth_works(client):
    """Verify header auth still works"""
    response = await client.get("/api/overview", headers={"X-Auth-Token": "test_secret_token"})
    assert response.status_code == 200
    assert "services" in await response.get_json()

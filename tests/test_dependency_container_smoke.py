"""Tests for the offline production-container health smoke."""

import pytest

from scripts.dependency_container_smoke import check_application_health


@pytest.mark.asyncio
async def test_container_smoke_exercises_the_real_application_health_route(monkeypatch) -> None:
    from app import web

    monkeypatch.setattr(web.database, "is_database_connected", lambda: True)

    result = await check_application_health()

    assert result["status"] == "healthy"
    assert result["service"] == "gemaibotv2"
    assert result["services"]["database"] == "connected"

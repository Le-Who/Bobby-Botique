"""Displayed limits and availability match the real Gemini admission counter."""

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.repos import metrics_repo
from app.runtime_settings import store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,count,expected_limit,available", [(20, 19, 20, False), (None, 250, None, True), (5, 4, 5, False)]
)
async def test_stats_use_runtime_override_including_unlimited(monkeypatch, limit, count, expected_limit, available):
    monkeypatch.setattr(
        store,
        "get_snapshot",
        AsyncMock(return_value=store.SettingsSnapshot(2, MappingProxyType({"model_limit:gemini-test": limit}))),
    )
    monkeypatch.setattr(
        metrics_repo,
        "settings",
        SimpleNamespace(AVAILABLE_MODELS=["gemini-test"], DAILY_LIMITS={}, LIMIT_THRESHOLD_PERCENT=0.95),
    )
    monkeypatch.setattr(
        metrics_repo,
        "db_query",
        AsyncMock(
            return_value=[
                {
                    "model_name": "gemini-test",
                    "daily_limit": 100,
                    "request_count": count,
                    "is_available": True,
                    "usage_percent": 1,
                }
            ]
        ),
    )
    row = (await metrics_repo.get_gemini_key_usage_stats())[0]
    assert row["daily_limit"] == expected_limit
    assert row["is_available"] is available
    assert row["usage_percent"] == (0 if limit is None else count / limit * 100)
    assert row["limit_source"] == "admin"
    assert row["availability_scope"] == "local_rpd"


@pytest.mark.asyncio
async def test_stats_include_internal_model_outside_public_catalog(monkeypatch):
    snapshot = store.SettingsSnapshot(
        2, MappingProxyType({"process:memory.extract": {"models": ("gemini-internal-new",)}})
    )
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(return_value=snapshot))
    monkeypatch.setattr(
        metrics_repo, "settings", SimpleNamespace(AVAILABLE_MODELS=[], DAILY_LIMITS={}, LIMIT_THRESHOLD_PERCENT=0.95)
    )
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(metrics_repo, "db_query", query)
    await metrics_repo.get_gemini_key_usage_stats()
    sql, params = query.await_args.args
    assert "gemini-internal-new" in params[2]
    assert "UNION" in sql and "usage_date = $2" in sql

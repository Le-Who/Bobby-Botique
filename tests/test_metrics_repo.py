"""Tests for monitoring queries in app.repos.metrics_repo."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.repos import metrics_repo


@pytest.mark.asyncio
async def test_tavily_usage_stats_uses_utc_month_at_boundary():
    instant = datetime(2026, 9, 1, 0, 30, tzinfo=UTC)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    query = AsyncMock(return_value=[])
    with (
        patch.object(metrics_repo, "datetime", FrozenDateTime),
        patch.object(metrics_repo, "db_query", query),
    ):
        await metrics_repo.get_tavily_key_usage_stats()

    assert query.await_args.args[1][2] == "2026-09"


@pytest.mark.asyncio
async def test_gemini_usage_stats_cross_joins_configured_models_to_include_zero_usage():
    query = AsyncMock(return_value=[])
    fake_settings = type(
        "FakeSettings",
        (),
        {
            "AVAILABLE_MODELS": ["gemini-3.7-flash", "gemini-3.6-flash"],
            "DAILY_LIMITS": {"gemini-3.7-flash": 100, "gemini-3.6-flash": 200},
            "LIMIT_THRESHOLD_PERCENT": 0.9,
        },
    )()

    with (
        patch.object(metrics_repo, "db_query", query),
        patch.object(metrics_repo, "settings", fake_settings),
    ):
        await metrics_repo.get_gemini_key_usage_stats()

    sql, params = query.await_args.args
    assert "CROSS JOIN configured_models" in sql
    assert "WHERE ku.model_name IS NOT NULL" not in sql
    assert params[2] == ["gemini-3.7-flash", "gemini-3.6-flash"]
    assert params[3] == [100, 200]

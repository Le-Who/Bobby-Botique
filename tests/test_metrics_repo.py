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

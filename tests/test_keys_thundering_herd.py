import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.repos.keys import KeyStatusManager


@pytest.mark.asyncio
async def test_thundering_herd_suspension(monkeypatch):
    manager = KeyStatusManager()
    key_hash = "fake_key_hash"
    model_name = "test_model"
    now = datetime(2026, 10, 9, 12, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr("app.repos.keys.datetime", Clock)
    query = AsyncMock(side_effect=[[{"failure_count": 2}], [], [{"failure_count": 3}], []])
    invalidation = AsyncMock()
    monkeypatch.setattr("app.repos.keys.db_query", query)
    monkeypatch.setattr("app.repos.keys.invalidate_key_cache", invalidation)

    await asyncio.gather(*(manager.suspend_key(key_hash, model_name, "quota", "Test") for _ in range(5)))

    assert query.await_count == 2
    select, upsert = query.await_args_list
    assert select.args == (
        "SELECT failure_count FROM key_model_status WHERE key_hash = $1 AND model_name = $2",
        (key_hash, model_name),
    )
    sql, parameters = upsert.args
    assert "INSERT INTO key_model_status" in sql
    assert "VALUES ($1, $2, 'suspended', $3, $4, $5, NOW())" in sql
    assert "status = 'suspended'" in sql
    assert parameters == (key_hash, model_name, datetime(2026, 10, 10, 7, tzinfo=UTC), 3, "Test")
    invalidation.assert_awaited_once_with(model_name)
    assert manager._suspension_cache == {f"{key_hash}:{model_name}": now}

    now += timedelta(seconds=4)
    await manager.suspend_key(key_hash, model_name, "quota", "ignored")
    assert query.await_count == 2

    now += timedelta(seconds=1)
    await manager.suspend_key(key_hash, model_name, "quota", "after cooldown")
    assert query.await_count == 4
    assert query.await_args_list[2] == select
    assert query.await_args_list[3].args[1] == (
        key_hash,
        model_name,
        datetime(2026, 10, 10, 7, tzinfo=UTC),
        4,
        "after cooldown",
    )
    assert invalidation.await_count == 2
    assert manager._suspension_cache[f"{key_hash}:{model_name}"] == now

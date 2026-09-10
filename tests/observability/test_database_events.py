from __future__ import annotations

import asyncio

import pytest

from app.database import DatabaseManager


class _Acquire:
    async def __aenter__(self):
        await asyncio.sleep(0.01)
        return object()

    async def __aexit__(self, *_args):
        return None


class _Pool:
    _closed = False

    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_database_event_separates_pool_wait_and_query_duration(monkeypatch):
    from app import database

    manager = DatabaseManager()
    monkeypatch.setattr(manager, "pool", _Pool())
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(database, "emit", lambda event, **fields: captured.append((event, fields)))

    async def operation(_connection):
        await asyncio.sleep(0.02)
        return [1, 2]

    result = await manager._execute_with_retry(
        "memory.fetch_candidates",
        operation,
        "SELECT secret_column FROM private_table WHERE token = $1",
        retries=0,
    )

    assert result == [1, 2]
    finished = captured[-1]
    assert finished[0] == "database.operation_finished"
    assert finished[1]["pool_wait_ms"] >= 5
    assert finished[1]["query_duration_ms"] >= 15
    assert finished[1]["row_count"] == 2
    assert "secret_column" not in repr(captured)
    assert len(finished[1]["statement_fingerprint"]) == 16

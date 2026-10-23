"""Board entries retain their object shape through the runtime JSONB codec."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from app import database
from app.repos import boards_repo


@pytest.mark.asyncio
async def test_board_append_sends_json_array_through_configured_codec(monkeypatch):
    codecs = {}

    class Connection:
        async def execute(self, _sql):
            return None

        async def set_type_codec(self, name, **kwargs):
            codecs[name] = kwargs

    async def create_pool(**kwargs):
        await kwargs["init"](Connection())
        return SimpleNamespace(_closed=False)

    manager = database.DatabaseManager()
    monkeypatch.setattr(database.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(manager, "_start_background_task", lambda *_args: None)
    await manager.create_pool()
    codec = codecs["jsonb"]

    async def query(_sql, params=()):
        encoded = codec["encoder"](params[1])
        with closing(sqlite3.connect(":memory:")) as connection:
            # PostgreSQL's JSONB || appends a scalar as one array element.
            # Check the actual wire shape before any legacy reader repairs it.
            assert connection.execute("SELECT json_type(?)", (encoded,)).fetchone() == ("array",)
        return [{"entries": codec["decoder"](encoded), "entries_since_last_synthesis": 1}]

    monkeypatch.setattr(boards_repo, "_TABLE_VERIFIED", True)
    monkeypatch.setattr(boards_repo.db, "db_query", query)
    monkeypatch.setattr(boards_repo.time, "time", lambda: 1234.0)

    entries = await boards_repo.add_entry(7, "Participant", "An idea")

    assert entries == [{"user": "Participant", "text": "An idea", "ts": 1234.0}]

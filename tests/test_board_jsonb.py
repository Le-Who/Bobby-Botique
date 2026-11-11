"""Board entries retain their object shape through the runtime JSONB codec."""

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import database
from app.handlers import board_handler
from app.repos import boards_repo


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "changed"])
async def test_summary_preserves_closed_or_newer_board(monkeypatch, state):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("NOW", 0, lambda: "synthetic-time")
        connection.create_function("jsonb_array_length", 1, lambda value: len(json.loads(value)))
        connection.execute(
            "CREATE TABLE inline_boards (id INTEGER PRIMARY KEY, last_summary TEXT, "
            "entries TEXT, entries_since_last_synthesis INTEGER, closed INTEGER, updated_at TEXT)"
        )
        entries = [{"text": "Synthetic first"}]
        if state == "changed":
            entries.append({"text": "Synthetic newer"})
        connection.execute(
            "INSERT INTO inline_boards VALUES (91, 'Original summary', ?, 1, ?, 'original-time')",
            (json.dumps(entries), int(state == "closed")),
        )

        async def query(sql, params=()):
            # Keep the production predicate; only PostgreSQL's scalar type cast
            # needs translation for this isolated SQL execution.
            sql = sql.replace("::int", "")
            bindings = {str(index): value for index, value in enumerate(params, start=1)}
            return [dict(row) for row in connection.execute(sql, bindings).fetchall()]

        monkeypatch.setattr(boards_repo, "_TABLE_VERIFIED", True)
        monkeypatch.setattr(boards_repo.db, "db_query", query)

        monkeypatch.setattr(board_handler, "_run_synthesis", AsyncMock(return_value="Stale summary"))
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        await board_handler._maybe_synthesize(
            bot,
            {"id": 91, "inline_msg_id": "synthetic-inline", "topic": "Synthetic topic"},
            [{"text": "Synthetic first"}],
            1,
            True,
        )
        row = connection.execute("SELECT last_summary, entries_since_last_synthesis FROM inline_boards").fetchone()
        assert tuple(row) == ("Original summary", 1)
        bot.edit_message_text.assert_not_awaited()


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

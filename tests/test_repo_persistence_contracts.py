"""Offline regression checks for persistence mapping and DB result contracts."""

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.entities import UserStateRow
from app.repos import conversations, users


@pytest.mark.asyncio
@pytest.mark.parametrize("diaries", [{"user_role:42": ["Первая запись", "Вторая запись"]}, {}])
async def test_loaded_user_state_keeps_persisted_role_diaries(monkeypatch, diaries):
    query = AsyncMock(return_value=[{"user_id": 42, "role_diaries": diaries}])
    monkeypatch.setattr(users, "db_query", query)

    loaded = await users.load_user_state(42)

    assert loaded is not None
    assert loaded["role_diaries"] == diaries
    assert query.await_args.args[1] == (42,)


def test_user_state_role_diary_defaults_are_independent():
    first = UserStateRow()
    second = UserStateRow()

    first.role_diaries["role:1"] = ["First state's diary"]

    assert second.role_diaries == {}


@pytest.mark.asyncio
async def test_loaded_legacy_user_state_has_empty_role_diaries(monkeypatch):
    monkeypatch.setattr(users, "db_query", AsyncMock(return_value=[{"user_id": 42}]))

    loaded = await users.load_user_state(42)

    assert loaded is not None
    assert loaded["role_diaries"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,expected", [([], False), ([{"id": 10}], True)])
async def test_rename_conversation_reports_whether_an_owned_row_changed(monkeypatch, rows, expected):
    query = AsyncMock(return_value=rows)
    monkeypatch.setattr(conversations, "db_query", query)

    result = await conversations.rename_conversation(42, 10, "New title")

    assert result is expected
    assert query.await_args.args[1] == ("New title", 10, 42)
    assert "RETURNING id" in query.await_args.args[0]


@pytest.mark.asyncio
async def test_saved_message_query_orders_equal_timestamps_by_id(monkeypatch):
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {"role": "user", "content": "First", "created_at": created_at},
        {"role": "model", "content": "Second", "created_at": created_at},
    ]
    query = AsyncMock(return_value=rows)
    monkeypatch.setattr(conversations, "db_query", query)

    messages = await conversations.get_conversation_messages(10, 42)

    assert messages == rows
    sql = re.sub(r"\s+", " ", query.await_args.args[0]).strip()
    assert "ORDER BY cm.created_at ASC, cm.id ASC" in sql
    assert query.await_args.args[1] == (10, 42)

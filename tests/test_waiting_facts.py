"""Tests for app.utils.waiting_facts — fun facts and waiting messages."""

from unittest.mock import AsyncMock

import pytest

from app.utils import waiting_facts
from app.utils.waiting_facts import FUN_FACTS, get_waiting_message


class TestFunFacts:
    """FUN_FACTS data integrity checks."""

    def test_not_empty(self):
        assert len(FUN_FACTS) > 0

    def test_all_strings(self):
        assert all(isinstance(f, str) for f in FUN_FACTS)

    def test_no_empty_strings(self):
        assert all(f.strip() for f in FUN_FACTS)


class TestGetWaitingMessage:
    """get_waiting_message should always return a non-empty string."""

    @pytest.mark.asyncio
    async def test_returns_string_without_user_id(self, monkeypatch):
        personalized = AsyncMock()
        monkeypatch.setattr(waiting_facts, "get_personalized_stat", personalized)
        monkeypatch.setattr(waiting_facts.random, "random", lambda: 0.0)
        monkeypatch.setattr(waiting_facts.random, "choice", lambda _facts: "A fixed fact")
        assert await get_waiting_message() == "💡 Знаете ли вы? A fixed fact"
        personalized.assert_not_awaited()

    @pytest.mark.parametrize(
        "draw,stat,expected,calls",
        [
            (0.8, None, "💡 Знаете ли вы? A fixed fact", 0),
            (0.0, "Personal stat", "💡 Немного статистики:\nPersonal stat", 1),
            (0.0, None, "💡 Знаете ли вы? A fixed fact", 1),
        ],
        ids=["static", "personalized", "personalized-empty"],
    )
    @pytest.mark.asyncio
    async def test_returns_string_with_user_id(self, monkeypatch, draw, stat, expected, calls):
        personalized = AsyncMock(return_value=stat)
        monkeypatch.setattr(waiting_facts, "get_personalized_stat", personalized)
        monkeypatch.setattr(waiting_facts.random, "random", lambda: draw)
        monkeypatch.setattr(waiting_facts.random, "choice", lambda _facts: "A fixed fact")
        assert await get_waiting_message(user_id=12345) == expected
        assert personalized.await_count == calls
        if calls:
            personalized.assert_awaited_once_with(12345)


@pytest.mark.asyncio
async def test_get_personalized_stat_handles_null_db_returns(monkeypatch):
    """
    Regression test for TypeError in get_personalized_stat.
    Tests that if SQL aggregates return NULL (which asyncpg maps to None),
    the function doesn't crash on `> 0` comparisons.
    """
    from app.utils.waiting_facts import get_personalized_stat

    # Clear cache to force DB lookup
    monkeypatch.setattr(waiting_facts, "_stat_cache", {})

    # Mock db_query to return records where the aggregate column is None
    # (simulating SUM() returning NULL for a user with no rows)
    async def mock_db_query(query, params):
        if "SIN(" in query or "MIN(" in query:
            return [{"first_seen": None}]
        elif "SUM(" in query:
            return [{"total": None}]
        elif "AND metric_date = " in query:
            return [{"request_count": None}]
        return []

    monkeypatch.setattr("app.utils.waiting_facts.db.db_query", mock_db_query)

    # This should not raise TypeError
    stat = await get_personalized_stat(user_id=99999)
    # Since all totals are treated as 0 or None, it shouldn't generate templates
    assert stat is None

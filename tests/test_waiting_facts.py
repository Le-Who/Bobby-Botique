"""Tests for app.utils.waiting_facts — fun facts and waiting messages."""

import pytest

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
    async def test_returns_string_without_user_id(self):
        msg = await get_waiting_message()
        assert isinstance(msg, str)
        assert len(msg) > 0

    @pytest.mark.asyncio
    async def test_returns_string_with_user_id(self):
        msg = await get_waiting_message(user_id=12345)
        assert isinstance(msg, str)
        assert len(msg) > 0


@pytest.mark.asyncio
async def test_get_personalized_stat_handles_null_db_returns(monkeypatch):
    """
    Regression test for TypeError in get_personalized_stat.
    Tests that if SQL aggregates return NULL (which asyncpg maps to None),
    the function doesn't crash on `> 0` comparisons.
    """
    from app.utils.waiting_facts import _stat_cache, get_personalized_stat

    # Clear cache to force DB lookup
    _stat_cache.clear()

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

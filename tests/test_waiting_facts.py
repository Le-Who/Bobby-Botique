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

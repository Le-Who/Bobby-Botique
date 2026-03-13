"""Tests for app.search_services — Tavily search input validation and parallel search dedup."""

import pytest

from app.search_services import parallel_search, tavily_search_agent

# ── Input validation ─────────────────────────────────────────────────────────


class TestTavilySearchValidation:
    """tavily_search_agent should validate inputs before making API calls."""

    @pytest.mark.asyncio
    async def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            await tavily_search_agent("")

    @pytest.mark.asyncio
    async def test_whitespace_query_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            await tavily_search_agent("   ")

    @pytest.mark.asyncio
    async def test_none_query_raises(self):
        with pytest.raises(ValueError):
            await tavily_search_agent(None)

    @pytest.mark.asyncio
    async def test_too_long_query_raises(self):
        with pytest.raises(ValueError, match="1000"):
            await tavily_search_agent("x" * 1001)

    @pytest.mark.asyncio
    async def test_invalid_search_type_raises(self):
        with pytest.raises(ValueError, match="search_type"):
            await tavily_search_agent("valid query", search_type="invalid")

    @pytest.mark.asyncio
    async def test_invalid_user_id_raises(self):
        with pytest.raises(ValueError, match="user_id"):
            await tavily_search_agent("valid query", user_id=-1)

    @pytest.mark.asyncio
    async def test_invalid_chat_id_type_raises(self):
        with pytest.raises(ValueError, match="chat_id"):
            await tavily_search_agent("valid query", chat_id="not_an_int")


# ── Parallel search deduplication ────────────────────────────────────────────


class TestParallelSearch:
    """parallel_search should handle empty inputs gracefully."""

    @pytest.mark.asyncio
    async def test_empty_queries_returns_empty(self):
        result = await parallel_search([])
        assert result == []

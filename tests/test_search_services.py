"""Tests for app.search_services — Tavily search input validation and parallel search dedup."""

from unittest.mock import AsyncMock, patch

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


class TestTavilySearchPayload:
    """Tavily request payloads should match the upstream API contract."""

    @pytest.mark.asyncio
    async def test_qna_requests_answer_in_payload(self):
        with (
            patch(
                "app.search_services.get_cached_search_result",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.search_services.get_available_tavily_key",
                new_callable=AsyncMock,
                return_value={"api_key": "secret", "key_hash": "hash"},
            ),
            patch(
                "app.search_services._tavily_api_call",
                new_callable=AsyncMock,
                return_value={"answer": "The answer"},
            ) as mock_api_call,
            patch(
                "app.search_services.increment_tavily_key_usage",
                new_callable=AsyncMock,
            ),
            patch(
                "app.search_services.cache_search_result",
                new_callable=AsyncMock,
            ),
            patch(
                "app.search_services.metrics_collector.record_search_query",
                new_callable=AsyncMock,
            ),
            patch(
                "app.search_services.metrics_collector.record_api_call",
                new_callable=AsyncMock,
            ),
        ):
            result = await tavily_search_agent("What is TDD?", search_type="qna")

        payload = mock_api_call.await_args.args[0]
        assert payload["include_answer"] is True
        assert result == {"type": "answer", "content": "The answer"}


# ── Parallel search deduplication ────────────────────────────────────────────


class TestParallelSearch:
    """parallel_search should handle empty inputs gracefully."""

    @pytest.mark.asyncio
    async def test_empty_queries_returns_empty(self):
        result = await parallel_search([])
        assert result == []

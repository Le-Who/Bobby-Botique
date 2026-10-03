"""Search-tool regression tests exercise current research evidence assembly."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.core import agentic


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(agentic, "get_cached_genai_client", lambda _: SimpleNamespace())
    return agentic.AgenticSearch(model_name="gemini-test", api_key="fake-key")


@pytest.mark.asyncio
async def test_search_tool_deduplicates_tracking_variants_and_keeps_first_evidence(agent, monkeypatch):
    search = AsyncMock(
        return_value=[
            {"url": "https://example.com/article", "content": "First evidence"},
            {"url": "https://www.example.com/article/?utm_source=news#section", "content": "Duplicate evidence"},
            {"url": "https://example.com/other", "content": "Other evidence"},
        ]
    )
    monkeypatch.setattr(agentic, "parallel_search", search)
    call = types.FunctionCall(name="search_web", args={"queries": ["query"]})
    result = await agent._execute_tool(call, seen_urls=set())

    assert [row["content"] for row in result["results"]] == ["First evidence", "Other evidence"]
    assert [row["url"] for row in result["results"]] == [
        "https://example.com/article",
        "https://example.com/other",
    ]
    assert result["_dedup_count"] == 1
    search.assert_awaited_once_with(["query"], user_id=None, chat_id=None, max_results=10)


@pytest.mark.asyncio
async def test_search_tool_excludes_seen_evidence_across_searches_but_keeps_distinct_pages(agent, monkeypatch):
    search = AsyncMock(
        side_effect=[
            [{"url": "https://example.com/article?page=1", "content": "Page one"}],
            [
                {"url": "https://example.com/article/?page=1&utm_medium=email", "content": "Repeated page one"},
                {"url": "https://example.com/article?page=2", "content": "Page two"},
            ],
        ]
    )
    monkeypatch.setattr(agentic, "parallel_search", search)
    seen = set()
    first = await agent._execute_tool(
        types.FunctionCall(name="search_web", args={"queries": ["first query"]}), seen_urls=seen
    )
    second = await agent._execute_tool(
        types.FunctionCall(name="search_web", args={"queries": ["second query"]}), seen_urls=seen
    )

    assert [row["content"] for row in first["results"]] == ["Page one"]
    assert [row["content"] for row in second["results"]] == ["Page two"]
    assert second["_dedup_count"] == 1
    assert seen == {"https://example.com/article?page=1", "https://example.com/article?page=2"}

"""Tests for the 5 agentic research improvements.

Covers:
  1. Parallel tool execution (asyncio.gather)
  2. Page content caching (session + global)
  3. Source quality scoring (domain classification, freshness, citation validation)
  4. Adaptive iteration budget (query dedup, token cap, time cutoff)
  5. Streaming progress (on_status receives detail kwarg)
"""

import time
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types

from app.core.agentic import (
    AgenticSearch,
    _classify_domain,
    _classify_freshness,
    _enrich_search_results,
    _find_duplicate_queries,
    _get_cached_page,
    _jaccard_similarity,
    _page_cache,
    _page_cache_times,
    _set_cached_page,
    _validate_citations,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_agent():
    with patch("app.core.agentic.get_cached_genai_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.aio = AsyncMock()
        mock_client.aio.models.generate_content = AsyncMock()
        mock_factory.return_value = mock_client
        agent = AgenticSearch(model_name="gemini-2.5-flash", api_key="fake-key")
        return agent


@pytest.fixture
def mock_status_callback():
    return AsyncMock()


@pytest.fixture(autouse=True)
def clear_page_cache():
    """Clear the global page cache between tests."""
    _page_cache.clear()
    _page_cache_times.clear()
    yield
    _page_cache.clear()
    _page_cache_times.clear()


# ── Improvement 3: Source quality scoring ─────────────────────────────────────


class TestDomainClassification:
    def test_official_docs_exact(self):
        dtype, tier = _classify_domain("https://docs.python.org/3/library/asyncio.html")
        assert dtype == "official_docs"
        assert tier == "A"

    def test_github_exact(self):
        dtype, tier = _classify_domain("https://github.com/python/cpython")
        assert dtype == "code_repository"
        assert tier == "A"

    def test_stackoverflow(self):
        dtype, tier = _classify_domain("https://stackoverflow.com/questions/123")
        assert dtype == "developer_qa"
        assert tier == "A"

    def test_reddit_community(self):
        dtype, tier = _classify_domain("https://www.reddit.com/r/python")
        assert dtype == "community"
        assert tier == "B"

    def test_subdomain_parent_match(self):
        dtype, tier = _classify_domain("https://old.reddit.com/r/python")
        assert dtype == "community"
        assert tier == "B"

    def test_trusted_prefix_docs(self):
        dtype, tier = _classify_domain("https://docs.someframework.dev/guide")
        assert dtype == "official_docs"
        assert tier == "A"

    def test_readthedocs(self):
        dtype, tier = _classify_domain("https://myproject.readthedocs.io/en/latest/")
        assert dtype == "official_docs"
        assert tier == "A"

    def test_unknown_domain(self):
        dtype, tier = _classify_domain("https://random-site.xyz/page")
        assert dtype == "unknown"
        assert tier == "C"

    def test_invalid_url(self):
        dtype, tier = _classify_domain("not-a-url")
        assert tier == "C"

    def test_wikipedia(self):
        dtype, tier = _classify_domain("https://en.wikipedia.org/wiki/Python")
        assert dtype == "encyclopedia"
        assert tier == "A"


class TestFreshnessClassification:
    def test_recent(self):
        from datetime import datetime, timedelta, timezone

        recent_date = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        assert _classify_freshness(recent_date) == "recent"

    def test_this_year(self):
        from datetime import datetime, timedelta, timezone

        six_months_ago = (datetime.now(UTC) - timedelta(days=180)).isoformat()
        assert _classify_freshness(six_months_ago) == "this_year"

    def test_older(self):
        assert _classify_freshness("2020-01-01T00:00:00Z") == "older"

    def test_none(self):
        assert _classify_freshness("") == "unknown"
        assert _classify_freshness("None") == "unknown"

    def test_invalid_date(self):
        assert _classify_freshness("not-a-date") == "unknown"


class TestEnrichSearchResults:
    def test_enrichment_adds_fields(self):
        results = [
            {"url": "https://github.com/foo/bar", "published_date": "2025-01-01T00:00:00Z"},
            {"url": "https://random-site.com/page"},
        ]
        enriched = _enrich_search_results(results)
        assert enriched[0]["domain_type"] == "code_repository"
        assert enriched[0]["quality_tier"] == "A"
        assert enriched[0]["freshness"] in ("this_year", "older")
        assert enriched[1]["quality_tier"] == "C"
        assert enriched[1]["freshness"] == "unknown"


class TestCitationValidation:
    def test_valid_citations(self):
        answer = "As noted in [Python docs](https://docs.python.org/3/), asyncio is great."
        known = {"https://docs.python.org/3/"}
        unknown = _validate_citations(answer, known)
        assert len(unknown) == 0

    def test_unknown_citation(self):
        answer = "See [this article](https://mystery.com/article) for details."
        known = {"https://docs.python.org/3/"}
        unknown = _validate_citations(answer, known)
        assert len(unknown) == 1
        assert "mystery.com" in unknown[0]

    def test_no_citations(self):
        answer = "This is a plain text answer."
        known = {"https://example.com"}
        unknown = _validate_citations(answer, known)
        assert len(unknown) == 0


# ── Improvement 4: Query deduplication & adaptive budget ─────────────────────


class TestJaccardSimilarity:
    def test_identical(self):
        assert _jaccard_similarity("python async tutorial", "python async tutorial") == 1.0

    def test_similar(self):
        sim = _jaccard_similarity("python async best practices", "python async common practices")
        assert sim > 0.5
        assert sim < 1.0

    def test_dissimilar(self):
        sim = _jaccard_similarity("python async tutorial", "rust ownership guide")
        assert sim < 0.3

    def test_empty(self):
        assert _jaccard_similarity("", "python") == 0.0


class TestFindDuplicateQueries:
    def test_no_duplicates(self):
        dupes = _find_duplicate_queries(["new query"], ["old query"])
        assert len(dupes) == 0

    def test_exact_duplicate(self):
        dupes = _find_duplicate_queries(["python async"], ["python async"])
        assert len(dupes) == 1

    def test_near_duplicate(self):
        # "python asyncio best practices guide" vs "python asyncio best practices tutorial"
        # Words: {python,asyncio,best,practices,guide} vs {python,asyncio,best,practices,tutorial}
        # Intersection=4, Union=6 → Jaccard=0.667
        dupes = _find_duplicate_queries(
            ["python asyncio best practices guide"],
            ["python asyncio best practices tutorial"],
            threshold=0.6,
        )
        assert len(dupes) == 1

    def test_all_unique(self):
        dupes = _find_duplicate_queries(
            ["python", "javascript"],
            ["rust", "go"],
        )
        assert len(dupes) == 0


# ── Improvement 2: Page content caching ──────────────────────────────────────


class TestPageCaching:
    def test_cache_set_and_get(self):
        _set_cached_page("https://example.com", "Hello World")
        assert _get_cached_page("https://example.com") == "Hello World"

    def test_cache_miss(self):
        assert _get_cached_page("https://nonexistent.com") is None

    def test_cache_expiry(self):
        """Simulate expired entry by manipulating the timestamp."""
        _set_cached_page("https://old.com", "Old content")
        import hashlib

        key = hashlib.sha256(b"https://old.com").hexdigest()
        # Set timestamp to 1 hour ago (far beyond 30-min TTL)
        _page_cache_times[key] = time.monotonic() - 3600
        assert _get_cached_page("https://old.com") is None

    def test_cache_maxsize_eviction(self):
        """Test that cache evicts oldest when full."""
        # Fill cache to 500
        for i in range(500):
            _set_cached_page(f"https://fill-{i}.com", f"Content {i}")
        assert len(_page_cache) == 500

        # Adding one more should evict the oldest
        _set_cached_page("https://new.com", "New content")
        assert len(_page_cache) == 500
        assert _get_cached_page("https://new.com") == "New content"


# ── Integration tests for the agentic loop improvements ──────────────────────


@pytest.mark.asyncio
async def test_on_status_receives_detail_kwarg(mock_agent, mock_status_callback):
    """Improvement 5: Verify on_status is called with detail kwarg for search_web."""
    # First response: call search_web
    search_call = MagicMock()
    search_call.name = "search_web"
    search_call.args = {"queries": ["test query"]}
    search_part = MagicMock(function_call=search_call, text=None)

    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[search_part]))]

    # Second response: conclude
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Done"}
    conclude_part = MagicMock(function_call=conclude_call, text=None)

    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[conclude_part]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2]

    with patch("app.core.agentic.parallel_search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [{"url": "http://test.com", "content": "data"}]

        await mock_agent.run("test", mock_status_callback)

    # Check that at least one on_status call has a 'detail' kwarg
    detail_calls = [c for c in mock_status_callback.call_args_list if c.kwargs.get("detail")]
    assert len(detail_calls) >= 1, "on_status should receive detail kwarg for search calls"
    assert "«test query»" in detail_calls[0].kwargs["detail"]


@pytest.mark.asyncio
async def test_query_dedup_sends_advisory(mock_agent, mock_status_callback):
    """Improvement 4: Verify duplicate queries get advisory response instead of execution."""
    mock_agent.max_iterations = 3

    # First response: search_web with "python async"
    search_call1 = MagicMock()
    search_call1.name = "search_web"
    search_call1.args = {"queries": ["python async"]}
    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=search_call1, text=None)]))]

    # Second response: search_web with the SAME query → should be deduped
    search_call2 = MagicMock()
    search_call2.name = "search_web"
    search_call2.args = {"queries": ["python async"]}
    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=search_call2, text=None)]))]

    # Third response: conclude
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Final"}
    resp3 = MagicMock()
    resp3.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=conclude_call, text=None)]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2, resp3]

    with patch("app.core.agentic.parallel_search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [{"url": "http://test.com", "content": "data"}]

        result = await mock_agent.run("test", mock_status_callback)

    assert result.answer == "Final"
    # parallel_search should only be called ONCE (the second identical query was deduped)
    assert mock_search.call_count == 1


@pytest.mark.asyncio
async def test_token_budget_forces_synthesis(mock_agent, mock_status_callback):
    """Improvement 4: Verify token budget cap forces synthesis."""
    mock_agent.max_tokens = 100  # Very low budget
    mock_agent.max_iterations = 5

    # First response: search_web (will generate tokens)
    search_call = MagicMock()
    search_call.name = "search_web"
    search_call.args = {"queries": ["test"]}
    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=search_call, text=None)]))]
    # Set up usage_metadata to return 200 tokens (exceeds budget of 100)
    resp1.usage_metadata = MagicMock()
    resp1.usage_metadata.total_token_count = 200

    # Synthesis response
    synth_resp = MagicMock()
    synth_resp.text = "Synthesized due to budget"
    synth_resp.usage_metadata = MagicMock()
    synth_resp.usage_metadata.total_token_count = 50

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, synth_resp]

    with patch("app.core.agentic.parallel_search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = []

        result = await mock_agent.run("test", mock_status_callback)

    # Should get synthesis answer because token budget was exceeded after first call
    assert "Synthesized due to budget" in result.answer


@pytest.mark.asyncio
async def test_session_page_cache_prevents_redundant_reads(mock_agent, mock_status_callback):
    """Improvement 2: Verify same URL in one session is only fetched once."""
    mock_agent.max_pages = 3
    mock_agent.max_iterations = 3

    # First response: read page
    read_call1 = MagicMock()
    read_call1.name = "read_page"
    read_call1.args = {"url": "http://same-page.com"}
    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=read_call1, text=None)]))]

    # Second response: read the SAME page again
    read_call2 = MagicMock()
    read_call2.name = "read_page"
    read_call2.args = {"url": "http://same-page.com"}
    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=read_call2, text=None)]))]

    # Third response: conclude
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Done with cached reads"}
    resp3 = MagicMock()
    resp3.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=conclude_call, text=None)]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2, resp3]

    with patch("app.core.agentic.read_url", new_callable=AsyncMock) as mock_read:
        mock_read.return_value = "Page content"

        result = await mock_agent.run("test", mock_status_callback)

    assert result.answer == "Done with cached reads"
    # read_url should only be called ONCE — second call hits session cache
    assert mock_read.call_count == 1


@pytest.mark.asyncio
async def test_parallel_execution_of_multiple_tools(mock_agent, mock_status_callback):
    """Improvement 1: Verify multiple tools in one response execute in parallel via gather."""
    mock_agent.max_pages = 3

    # Response with search_web + read_page in the same turn
    search_call = MagicMock()
    search_call.name = "search_web"
    search_call.args = {"queries": ["query1"]}
    search_part = MagicMock(function_call=search_call, text=None)

    read_call = MagicMock()
    read_call.name = "read_page"
    read_call.args = {"url": "http://page1.com"}
    read_part = MagicMock(function_call=read_call, text=None)

    resp1 = MagicMock()
    resp1.candidates = [MagicMock(content=MagicMock(parts=[search_part, read_part]))]

    # Conclude
    conclude_call = MagicMock()
    conclude_call.name = "conclude_research"
    conclude_call.args = {"answer": "Parallel done"}
    resp2 = MagicMock()
    resp2.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(function_call=conclude_call, text=None)]))]

    mock_agent.client.aio.models.generate_content.side_effect = [resp1, resp2]

    with (
        patch("app.core.agentic.parallel_search", new_callable=AsyncMock) as mock_search,
        patch("app.core.agentic.read_url", new_callable=AsyncMock) as mock_read,
    ):
        mock_search.return_value = [{"url": "http://test.com"}]
        mock_read.return_value = "Page content"

        result = await mock_agent.run("test", mock_status_callback)

    assert result.answer == "Parallel done"
    mock_search.assert_called_once()
    mock_read.assert_called_once()

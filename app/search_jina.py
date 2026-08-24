"""JINA Search grounding module — lightweight search for Opencode Go models.

Uses s.jina.ai for web search (returns LLM-ready markdown) and r.jina.ai for
reading individual pages. Replaces Gemini-native Google Search Grounding for
the Opencode Go provider path.

Key design decisions:
- Uses the JINA_API_KEY for authenticated requests (higher rate limits).
- Falls back gracefully if JINA is unavailable (returns empty string).
- Returns clean markdown that can be injected into the system prompt.
- ``search_for_grounding()`` is the primary public API.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple
from urllib.parse import quote as url_quote

import httpx

_JINA_SEARCH_BASE = "https://s.jina.ai/"
_JINA_READER_BASE = "https://r.jina.ai/"
_DEFAULT_TIMEOUT = 15.0
_MAX_RESULT_CHARS = 12_000  # Truncation cap for the combined search context


class JinaSearchResult(NamedTuple):
    query: str
    content: str  # Markdown-formatted search results
    source_urls: list[str]


def _build_jina_headers(api_key: str | None = None) -> dict[str, str]:
    """Build request headers for JINA API calls."""
    headers: dict[str, str] = {
        "Accept": "text/event-stream",  # JINA's streaming markdown format
        "X-With-URLs-On-Screen": "true",  # Include source URLs in output
        "X-No-Cache": "false",  # Allow cache for grounding (often same queries)
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _extract_source_urls(content: str) -> list[str]:
    """Extract URLs from JINA markdown output."""
    return re.findall(r"https?://[^\s\)\]\"']+", content)


async def search_jina(query: str, timeout: float = _DEFAULT_TIMEOUT) -> JinaSearchResult:
    """Perform a JINA Search query and return structured results.

    Uses the ``s.jina.ai`` search API which returns LLM-ready markdown
    including titles, snippets, and source URLs.

    Args:
        query: The search query string.
        timeout: Connection/read timeout in seconds.

    Returns:
        ``JinaSearchResult`` with query, content (markdown), and source URLs.
        On any error, returns a result with empty content.
    """
    from app.repos.provider_keys import get_provider_key

    api_key = await get_provider_key("jina")

    encoded_query = _JINA_SEARCH_BASE + url_quote(query, safe="")
    url = encoded_query

    headers = _build_jina_headers(api_key or None)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            content = resp.text[:_MAX_RESULT_CHARS]
    except httpx.TimeoutException:
        logging.warning("JINA Search timed out (query_length=%d)", len(query))
        return JinaSearchResult(query=query, content="", source_urls=[])
    except httpx.HTTPStatusError as e:
        logging.warning("JINA Search HTTP error %d (query_length=%d)", e.response.status_code, len(query))
        return JinaSearchResult(query=query, content="", source_urls=[])
    except Exception as e:
        logging.error("JINA Search unexpected error (error_type=%s, query_length=%d)", type(e).__name__, len(query))
        return JinaSearchResult(query=query, content="", source_urls=[])

    source_urls = _extract_source_urls(content)
    return JinaSearchResult(query=query, content=content, source_urls=source_urls)


async def read_jina_page(url: str, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Read a single page via JINA Reader (r.jina.ai).

    Returns LLM-ready markdown of the page, or empty string on failure.
    Retained for compatibility with the agentic research pipeline.

    Args:
        url: Full URL to read.
        timeout: Connection/read timeout in seconds.

    Returns:
        Markdown content of the page, truncated to ``_MAX_RESULT_CHARS``.
    """
    from app.repos.provider_keys import get_provider_key

    api_key = await get_provider_key("jina")

    reader_url = _JINA_READER_BASE + url
    headers = _build_jina_headers(api_key or None)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(reader_url, headers=headers)
            resp.raise_for_status()
            return resp.text[:_MAX_RESULT_CHARS]
    except httpx.TimeoutException:
        logging.warning("JINA Reader timed out")
        return ""
    except httpx.HTTPStatusError as e:
        logging.warning("JINA Reader HTTP error %d", e.response.status_code)
        return ""
    except Exception as e:
        logging.error("JINA Reader unexpected error (error_type=%s)", type(e).__name__)
        return ""


async def search_for_grounding(
    query: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str:
    """High-level grounding helper: query JINA Search and return formatted context.

    Designed for injection into the system prompt before chat completions.
    Returns a compact ``<search_context>`` block that includes the top results
    and their source URLs.

    Returns empty string if JINA is unavailable or returns no results, so the
    caller can proceed without web context rather than failing.
    """
    result = await search_jina(query, timeout=timeout)

    if not result.content:
        return ""

    sources = ""
    if result.source_urls:
        sources = "\nSources: " + " | ".join(result.source_urls[:5])

    return f"<search_context>\nSearch query: {result.query}\n\n{result.content}{sources}\n</search_context>"

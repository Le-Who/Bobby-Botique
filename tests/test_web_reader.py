"""Tests for app.web_reader — URL reading and truncation."""

from unittest.mock import AsyncMock

import httpx
import pytest
from tenacity import wait_none

from app import web_reader
from app.web_reader import MAX_PAGE_CHARS


class TestWebReaderConstants:
    """Verify web reader configuration constants."""

    def test_max_page_chars_reasonable(self):
        assert MAX_PAGE_CHARS > 1000
        assert MAX_PAGE_CHARS < 100_000


@pytest.fixture
def fake_http(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(web_reader._client, "get", get)
    monkeypatch.setattr("app.repos.provider_keys.get_provider_key", AsyncMock(return_value=None))
    monkeypatch.setattr(web_reader._fetch_jina.retry, "wait", wait_none())
    monkeypatch.setattr(web_reader._fetch_jina.retry, "sleep", AsyncMock())
    return get


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1, 500], ids=["at_cap", "over_cap", "large"])
async def test_read_url_caps_success_content(fake_http, extra):
    content = "x" * (MAX_PAGE_CHARS + extra)
    request = httpx.Request("GET", "https://r.jina.ai/https://example.test/article")
    fake_http.return_value = httpx.Response(200, text=content, request=request)
    result = await web_reader.read_url("https://example.test/article", timeout=2.5)
    suffix = "\n\n[...truncated due to length]" if extra else ""
    assert result == content[:MAX_PAGE_CHARS] + suffix
    fake_http.assert_awaited_once_with(
        "https://r.jina.ai/https://example.test/article",
        headers={"Accept": "text/markdown", "X-No-Cache": "true"},
        timeout=2.5,
    )


@pytest.mark.asyncio
async def test_read_url_retries_request_error_then_returns_success(fake_http):
    request = httpx.Request("GET", "https://example.test")
    fake_http.side_effect = [
        httpx.ConnectError("unavailable", request=request),
        httpx.Response(200, text="Recovered", request=request),
    ]
    assert await web_reader.read_url("https://example.test") == "Recovered"
    assert fake_http.await_count == 2
    assert fake_http.await_args_list[0] == fake_http.await_args_list[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected,attempts",
    [
        (httpx.ReadTimeout("timeout"), "[Error: Timeout reading URL]", 3),
        (httpx.ConnectError("unavailable"), "[Error: Failed to read URL]", 3),
        (RuntimeError("malformed response"), "[Error: Failed to read URL]", 1),
    ],
    ids=["timeout", "request_error", "unexpected_error"],
)
async def test_read_url_controlled_fetch_errors(fake_http, failure, expected, attempts):
    fake_http.side_effect = failure
    assert await web_reader.read_url("https://example.test") == expected
    assert fake_http.await_count == attempts


@pytest.mark.asyncio
async def test_read_url_http_status_error_retries_and_reports_status(fake_http):
    request = httpx.Request("GET", "https://example.test")
    fake_http.return_value = httpx.Response(503, text="unavailable", request=request)
    assert await web_reader.read_url("https://example.test") == "[Error: HTTP 503 reading URL]"
    assert fake_http.await_count == 3

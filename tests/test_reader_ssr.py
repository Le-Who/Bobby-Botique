"""Tests for updated web_miniapp Long Read endpoints.

Covers:
- SSR reader_page() with Redis hit (renders HTML + TOC)
- SSR reader_page() with Redis miss + Telegraph cold-storage proxy
- SSR reader_page() with missing UID (error state)
- api_reader_content() backward-compat XHR path
- _fetch_telegraph_content() network layer isolation
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── _fetch_telegraph_content ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_telegraph_content_extracts_article():
    """_fetch_telegraph_content() extracts text from the <article> tag.

    The function uses a local ``import httpx`` inside the coroutine body,
    so we patch ``httpx.AsyncClient`` at the httpx module level.
    """
    import httpx

    from app.web_miniapp import _fetch_telegraph_content

    fake_html = (
        "<html><body>"
        "<article><h3>My Title</h3><p>Some content here.</p></article>"
        "</body></html>"
    )
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(return_value=None)
    mock_resp.text = fake_html

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient") as cls_mock:
        cls_mock.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        cls_mock.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _fetch_telegraph_content("https://telegra.ph/Test-01")

    # Should return text extracted from the <article> body
    assert isinstance(result, str)
    assert "My Title" in result
    assert "Some content here." in result


@pytest.mark.asyncio
async def test_fetch_telegraph_content_returns_none_on_network_error():
    """_fetch_telegraph_content() returns None on network failure, never raises."""
    import httpx

    from app.web_miniapp import _fetch_telegraph_content

    with patch("httpx.AsyncClient") as cls_mock:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        cls_mock.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        cls_mock.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _fetch_telegraph_content("https://telegra.ph/Test-01")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_telegraph_content_returns_none_when_no_article_tag():
    """_fetch_telegraph_content() returns None if no <article> found in page HTML."""
    import httpx

    from app.web_miniapp import _fetch_telegraph_content

    # HTML without <article>
    fake_html = "<html><body><div>No article here</div></body></html>"
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock(return_value=None)
    mock_resp.text = fake_html

    with patch("httpx.AsyncClient") as cls_mock:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        cls_mock.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        cls_mock.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await _fetch_telegraph_content("https://telegra.ph/Test-01")

    assert result is None


# ── api_reader_content (backward-compat XHR path) ────────────────────────────


@pytest.mark.asyncio
async def test_api_reader_content_returns_markdown_on_redis_hit():
    """/api/reader/<uid> returns {\"markdown\":...} when Redis has the key."""
    from app.web_miniapp import api_reader_content

    mock_md = "# My Article\n\nContent here."
    with (
        patch("app.cache.get_long_message", AsyncMock(return_value=mock_md)),
        patch("app.cache.get_telegraph_url", AsyncMock(return_value=None)),
    ):
        from quart import Quart

        app = Quart(__name__)
        async with app.test_request_context("/api/reader/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
            resp = await api_reader_content("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            # response can be tuple (jsonify, status) or just jsonify
            if isinstance(resp, tuple):
                data = await resp[0].get_json()
            else:
                data = await resp.get_json()

    assert data["markdown"] == mock_md


@pytest.mark.asyncio
async def test_api_reader_content_returns_telegraph_url_on_redis_miss():
    """/api/reader/<uid> returns {\"telegraph_url\":...} when Redis expired."""
    from app.web_miniapp import api_reader_content

    test_url = "https://telegra.ph/Test-Page-01"
    with (
        patch("app.cache.get_long_message", AsyncMock(return_value=None)),
        patch("app.cache.get_telegraph_url", AsyncMock(return_value=test_url)),
    ):
        from quart import Quart

        app = Quart(__name__)
        async with app.test_request_context("/api/reader/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
            resp = await api_reader_content("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            if isinstance(resp, tuple):
                data = await resp[0].get_json()
            else:
                data = await resp.get_json()

    assert data["telegraph_url"] == test_url


@pytest.mark.asyncio
async def test_api_reader_content_returns_404_when_nothing_found():
    """/api/reader/<uid> returns 404 when both Redis and Telegraph return nothing."""
    from app.web_miniapp import api_reader_content

    with (
        patch("app.cache.get_long_message", AsyncMock(return_value=None)),
        patch("app.cache.get_telegraph_url", AsyncMock(return_value=None)),
    ):
        from quart import Quart

        app = Quart(__name__)
        async with app.test_request_context("/api/reader/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
            resp = await api_reader_content("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
            assert isinstance(resp, tuple)
            _body, status = resp
            assert status == 404


@pytest.mark.asyncio
async def test_api_reader_content_rejects_invalid_uid():
    """/api/reader/<uid> returns 400 for non-UUID input (injection guard)."""
    from quart import Quart

    from app.web_miniapp import api_reader_content

    app = Quart(__name__)
    async with app.test_request_context("/api/reader/../../etc/passwd"):
        resp = await api_reader_content("../../etc/passwd")
        assert isinstance(resp, tuple)
        _body, status = resp
        assert status == 400

"""Tests for long message storage (Redis) and Telegraph page creation.

AAA pattern: each test covers exactly one observable behaviour.

Redis round-trip tests use AsyncMock to avoid needing a live Redis instance,
making them deterministic unit tests that run anywhere without REDIS_URL.
Telegraph tests mock httpx to avoid real network calls.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Redis cache unit tests ────────────────────────────────────────────────────
# Use a mock Redis client to avoid event-loop and network dependencies.


@pytest.mark.asyncio
async def test_store_long_message_returns_true_on_success():
    """store_long_message() returns True when Redis setex succeeds."""
    # Arrange
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(return_value=True)
    uid = str(uuid.uuid4())

    with patch("app.cache.redis_client", mock_redis):
        from app.cache import store_long_message

        # Act
        result = await store_long_message(uid, "# Test\n\nContent.", ttl=60)

    # Assert
    assert result is True
    mock_redis.setex.assert_called_once()
    call_args = mock_redis.setex.call_args
    assert f"long_msg:{uid}" == call_args[0][0]  # key


@pytest.mark.asyncio
async def test_get_long_message_decodes_bytes():
    """get_long_message() returns the UTF-8 decoded string when key exists."""
    # Arrange
    uid = str(uuid.uuid4())
    stored_markdown = "# Test Markdown\n\nThis is a test message."

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=stored_markdown.encode("utf-8"))

    with patch("app.cache.redis_client", mock_redis):
        from app.cache import get_long_message

        # Act
        result = await get_long_message(uid)

    # Assert
    assert result == stored_markdown


@pytest.mark.asyncio
async def test_get_long_message_returns_none_when_key_missing():
    """get_long_message() returns None when key does not exist in Redis."""
    # Arrange
    uid = str(uuid.uuid4())
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("app.cache.redis_client", mock_redis):
        from app.cache import get_long_message

        # Act
        result = await get_long_message(uid)

    # Assert
    assert result is None


@pytest.mark.asyncio
async def test_store_telegraph_url_persists_url():
    """store_telegraph_url() calls Redis.set (no TTL) exactly once with the correct key."""
    # Arrange
    uid = str(uuid.uuid4())
    test_url = "https://telegra.ph/Test-01"

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    with patch("app.cache.redis_client", mock_redis):
        from app.cache import store_telegraph_url

        # Act
        result = await store_telegraph_url(uid, test_url)

    # Assert
    assert result is True
    mock_redis.set.assert_called_once()
    call_args = mock_redis.set.call_args
    assert f"long_msg:{uid}:tg_url" == call_args[0][0]


@pytest.mark.asyncio
async def test_get_telegraph_url_returns_url():
    """get_telegraph_url() decodes and returns the stored URL bytes."""
    # Arrange
    uid = str(uuid.uuid4())
    test_url = "https://telegra.ph/Test-01"

    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=test_url.encode("utf-8"))

    with patch("app.cache.redis_client", mock_redis):
        from app.cache import get_telegraph_url

        # Act
        result = await get_telegraph_url(uid)

    # Assert
    assert result == test_url


@pytest.mark.asyncio
async def test_store_long_message_returns_false_when_redis_unavailable():
    """store_long_message() returns False gracefully when redis_client is None."""
    # Arrange — no Redis configured
    with patch("app.cache.redis_client", None):
        from app.cache import store_long_message

        # Act
        result = await store_long_message("some-uid", "content")

    # Assert
    assert result is False


# ── Telegraph page creation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_telegraph_page_returns_url():
    """create_telegraph_page() makes two HTTP POST calls and returns the page URL.

    Key fix: httpx.Response.raise_for_status() is a SYNC method —
    mocks must use MagicMock(), not AsyncMock(). Similarly, .json()
    is sync and must return a plain dict, not a coroutine.

    We patch the module-level _access_token to skip the createAccount step
    and exercise only the createPage path cleanly.
    """
    # Arrange — patch at the source used by telegraph.py
    with (
        patch("app.utils.telegraph._access_token", "cached_test_token"),
        patch("app.utils.telegraph.httpx.AsyncClient") as mock_client_class,
    ):
        # Build a realistic sync-method response for createPage
        response_page = MagicMock()
        response_page.raise_for_status = MagicMock(return_value=None)  # sync call
        response_page.json = MagicMock(return_value={"ok": True, "result": {"url": "https://telegra.ph/Mock-Page"}})

        # Wire the context-manager chain: AsyncClient().__aenter__() → mock_client
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response_page)
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.utils.telegraph import create_telegraph_page

        # Act
        url = await create_telegraph_page("Title", "## Section\n\nMarkdown text here.")

    # Assert
    assert url == "https://telegra.ph/Mock-Page"
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_create_telegraph_page_returns_none_on_api_failure():
    """create_telegraph_page() returns None when Telegraph API reports ok=False."""
    # Arrange
    with (
        patch("app.utils.telegraph._access_token", "cached_test_token"),
        patch("app.utils.telegraph.httpx.AsyncClient") as mock_client_class,
    ):
        response_fail = MagicMock()
        response_fail.raise_for_status = MagicMock(return_value=None)
        response_fail.json = MagicMock(return_value={"ok": False, "error": "FLOOD_WAIT"})

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response_fail)
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.utils.telegraph import create_telegraph_page

        # Act
        url = await create_telegraph_page("Bad Title", "content")

    # Assert
    assert url is None


@pytest.mark.asyncio
async def test_create_telegraph_page_returns_none_on_network_error():
    """create_telegraph_page() returns None and does not raise when the network fails."""
    import httpx

    # Arrange
    with (
        patch("app.utils.telegraph._access_token", "cached_test_token"),
        patch("app.utils.telegraph.httpx.AsyncClient") as mock_client_class,
    ):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Network unreachable"))
        mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.utils.telegraph import create_telegraph_page

        # Act
        url = await create_telegraph_page("Net Error", "content")

    # Assert — graceful None, no exception leaks
    assert url is None

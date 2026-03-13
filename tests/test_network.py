"""Tests for app.utils.network — retry logic with exponential backoff."""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.utils.network import NetworkErrorHandler


class TestRetryWithBackoff:
    """NetworkErrorHandler.retry_with_backoff should retry on transient errors."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        func = AsyncMock(return_value="ok")
        result = await NetworkErrorHandler.retry_with_backoff(func, max_retries=3)
        assert result == "ok"
        assert func.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error_then_succeeds(self):
        func = AsyncMock(side_effect=[httpx.ConnectError("fail"), httpx.ConnectError("fail"), "ok"])
        result = await NetworkErrorHandler.retry_with_backoff(func, max_retries=3, base_delay=0.01)
        assert result == "ok"
        assert func.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        func = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        with pytest.raises(httpx.TimeoutException):
            await NetworkErrorHandler.retry_with_backoff(func, max_retries=2, base_delay=0.01)
        assert func.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_non_matching_exception_not_retried(self):
        func = AsyncMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            await NetworkErrorHandler.retry_with_backoff(
                func,
                max_retries=3,
                base_delay=0.01,
                exceptions=(httpx.TimeoutException,),
            )
        assert func.call_count == 1


class TestCreateRobustHttpClient:
    """create_robust_http_client should produce a configured httpx.AsyncClient."""

    def test_returns_async_client(self):
        client = NetworkErrorHandler.create_robust_http_client()
        assert isinstance(client, httpx.AsyncClient)

    def test_custom_timeouts(self):
        client = NetworkErrorHandler.create_robust_http_client(connect_timeout=5.0, read_timeout=15.0)
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 15.0

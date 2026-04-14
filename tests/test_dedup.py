"""Tests for app.middleware.dedup — request deduplication middleware."""

import time

import pytest

from app.middleware.dedup import (
    DEDUP_WINDOW_SECONDS,
    _hash_request,
    _recent_requests,
    clear_user_dedup,
    is_duplicate_request,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset dedup state between tests."""
    _recent_requests.clear()
    yield
    _recent_requests.clear()


class TestHashRequest:
    def test_deterministic(self):
        assert _hash_request("hello") == _hash_request("hello")

    def test_different_inputs(self):
        assert _hash_request("hello") != _hash_request("world")

    def test_length(self):
        assert len(_hash_request("test")) == 12


class TestIsDuplicateRequest:
    @pytest.mark.asyncio
    async def test_first_request_not_duplicate(self):
        assert await is_duplicate_request(1, "hello") is False

    @pytest.mark.asyncio
    async def test_same_request_within_window_is_duplicate(self):
        assert await is_duplicate_request(1, "hello") is False
        assert await is_duplicate_request(1, "hello") is True

    @pytest.mark.asyncio
    async def test_different_text_not_duplicate(self):
        assert await is_duplicate_request(1, "hello") is False
        assert await is_duplicate_request(1, "world") is False

    @pytest.mark.asyncio
    async def test_different_users_not_duplicate(self):
        assert await is_duplicate_request(1, "hello") is False
        assert await is_duplicate_request(2, "hello") is False

    @pytest.mark.asyncio
    async def test_empty_text_not_duplicate(self):
        assert await is_duplicate_request(1, "") is False
        assert await is_duplicate_request(1, "   ") is False

    @pytest.mark.asyncio
    async def test_request_after_window_not_duplicate(self):
        assert await is_duplicate_request(1, "hello") is False

        # Simulate time passing beyond the window
        user_hashes = _recent_requests[1]
        for h in user_hashes:
            user_hashes[h] = time.monotonic() - DEDUP_WINDOW_SECONDS - 1

        assert await is_duplicate_request(1, "hello") is False

    @pytest.mark.asyncio
    async def test_whitespace_stripping(self):
        """Leading/trailing whitespace should not affect dedup."""
        assert await is_duplicate_request(1, "  hello  ") is False
        assert await is_duplicate_request(1, "hello") is True

    @pytest.mark.asyncio
    async def test_eviction_when_over_limit(self):
        """Oldest hashes should be evicted when limit exceeded."""
        # Fill to max + 1
        for i in range(25):
            await is_duplicate_request(1, f"msg-{i}")

        # Should have been capped
        assert len(_recent_requests[1]) <= 21  # 20 max + 1 before eviction


class TestClearUserDedup:
    @pytest.mark.asyncio
    async def test_clear_removes_user_state(self):
        await is_duplicate_request(1, "hello")
        assert 1 in _recent_requests

        clear_user_dedup(1)
        assert 1 not in _recent_requests

    def test_clear_nonexistent_user_no_error(self):
        clear_user_dedup(9999)  # Should not raise

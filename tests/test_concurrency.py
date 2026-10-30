"""Concurrency and load stress tests.

Tests that critical subsystems behave correctly under concurrent access:
- Cache stampede resistance
- Admin alert rate limiter under concurrent load
- Telegram render-session rapid writes
- AgentRequestUseCase concurrent key resolution
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.concurrency import GlobalLLMSemaphore
from app.errors import UserLimitExceededError


@pytest.mark.asyncio
async def test_local_and_redis_wait_share_one_admission_deadline(monkeypatch):
    """A 119-second local wait leaves one second of the 120-second budget."""
    clock = SimpleNamespace(now=0.0)
    real_sleep = asyncio.sleep

    class DelayedLocalSemaphore(asyncio.Semaphore):
        async def acquire(self):
            result = await super().acquire()
            clock.now += 119
            return result

    async def advance_clock(seconds):
        clock.now += seconds
        await real_sleep(0)

    redis = AsyncMock()
    redis.eval.return_value = 0
    semaphore = GlobalLLMSemaphore(1, timeout=120)
    semaphore._local_semaphore = DelayedLocalSemaphore(1)
    monkeypatch.setattr("app.cache.redis_client", redis)
    monkeypatch.setattr(
        "app.adapters.concurrency.time",
        SimpleNamespace(monotonic=lambda: clock.now, monotonic_ns=time.monotonic_ns),
    )
    monkeypatch.setattr(asyncio, "sleep", advance_clock)

    with pytest.raises(UserLimitExceededError):
        async with semaphore:
            pytest.fail("exhausted admission must not enter provider work")

    assert clock.now == 120
    assert semaphore._local_semaphore._value == 1
    assert semaphore._waiting_count == 0


@pytest.mark.asyncio
async def test_stalled_redis_eval_is_bounded_by_admission_deadline(monkeypatch):
    """Socket/retry stalls cannot overrun the whole admission budget."""
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked_admission(*args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    redis = AsyncMock()
    redis.eval.side_effect = blocked_admission
    semaphore = GlobalLLMSemaphore(1, timeout=0.03)
    monkeypatch.setattr("app.cache.redis_client", redis)

    async def acquire():
        async with semaphore:
            pytest.fail("a timed out Redis call cannot become local fallback")

    task = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert task in done, "the Redis await must obey the admission deadline"
        with pytest.raises(UserLimitExceededError):
            await task
        assert stopped.is_set()
        assert semaphore._local_semaphore._value == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ============================================================================
# 1. CACHE STAMPEDE TEST
# ============================================================================


class TestCacheStampede:
    """Verify that concurrent cache misses don't cause duplicate computations."""

    @pytest.mark.asyncio
    async def test_multi_layer_cache_concurrent_reads(self):
        """Concurrent reads on the same key should not crash or corrupt data."""
        from app.cache import MultiLayerCache

        cache = MultiLayerCache()

        with patch("app.cache.redis_client", None):
            # Pre-populate
            await cache.set("test_key", "search", {"result": "hello"})

            # Fire 50 concurrent reads
            async def read():
                return await cache.get("test_key", "search")

            results = await asyncio.gather(*[read() for _ in range(50)])

            # All 50 should return the same cached value
            for r in results:
                assert r is not None
                assert r["result"] == "hello"

    @pytest.mark.asyncio
    async def test_multi_layer_cache_concurrent_writes(self):
        """Concurrent writes should not corrupt the in-memory cache."""
        from app.cache import MultiLayerCache

        cache = MultiLayerCache()

        # Patch redis_client to None to test only in-memory layer
        with patch("app.cache.redis_client", None):

            async def write(i: int):
                await cache.set(f"key_{i}", "search", {"value": i})

            # Fire 50 concurrent writes
            await asyncio.gather(*[write(i) for i in range(50)])

            # Verify all writes succeeded in memory
            for i in range(50):
                result = await cache.get(f"key_{i}", "search")
                assert result is not None
                assert result["value"] == i


# ============================================================================
# 2. ADMIN ALERT RATE LIMITER UNDER LOAD
# ============================================================================


class TestAlertRateLimiterConcurrent:
    """Verify rate limiter correctness under concurrent alert calls."""

    @pytest.mark.asyncio
    async def test_concurrent_alerts_respect_rate_limit(self):
        """Firing many alerts concurrently should still respect the 5/5min limit."""
        from app.admin_alerts import (
            _MAX_ALERTS,
            AlertSeverity,
            _alert_timestamps,
            alert_admin,
        )

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            # Fire 20 concurrent alerts
            tasks = [alert_admin(mock_app, f"Alert {i}", AlertSeverity.WARNING) for i in range(20)]
            await asyncio.gather(*tasks)

        # Should have sent at most _MAX_ALERTS (5)
        assert mock_app.bot.send_message.call_count <= _MAX_ALERTS

        _alert_timestamps.clear()


# ============================================================================
# 3. STREAMING WRITER CONCURRENT WRITES
# ============================================================================


class TestTelegramRenderSessionWrites:
    """Verify rapid typed renderer appends preserve the authoritative final text."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_produce_complete_output(self):
        """All chunks written concurrently should appear in the final output."""
        from app.response_delivery.renderer import TelegramMessageRef, TelegramRenderer

        class Transport:
            current_ref = TelegramMessageRef(chat_id=123, message_id=1)

            def __init__(self):
                self.edits = []

            async def edit(self, text, *, parse_mode, reply_markup):
                self.edits.append(text)

            async def send(self, text, *, parse_mode, reply_markup):
                raise AssertionError("short response must not need send recovery")

        transport = Transport()
        session = TelegramRenderer(
            transport,
            debounce_seconds=0,
            min_chunk_size=1,
        ).open()

        # Write 20 chunks as fast as possible
        for i in range(20):
            await session.append(f"chunk{i} ")

        final_text = "".join(f"chunk{i} " for i in range(20))
        await session.finalize(
            displayed_text=final_text,
            title="Answer",
            actions=None,
        )

        # The full text should contain all chunks
        for i in range(20):
            assert f"chunk{i}" in transport.edits[-1]


# ============================================================================
# 4. KEY RESOLUTION CONCURRENT ACCESS
# ============================================================================


class TestKeyResolutionConcurrent:
    """Verify key resolution handles concurrent requests correctly."""

    @pytest.mark.asyncio
    async def test_concurrent_resolve_requests(self):
        """Multiple concurrent resolve_ai_request calls should each get valid results."""
        from app.agent_use_cases import AgentRequestUseCase

        call_count = 0

        async def mock_get_key(model, excluded_hashes=None):
            nonlocal call_count
            call_count += 1
            # Simulate slight async delay
            await asyncio.sleep(0.001)
            return {"api_key": f"key_{call_count}", "key_hash": f"hash_{call_count}"}

        use_case = AgentRequestUseCase()

        # Fire 10 concurrent key resolutions
        tasks = [
            use_case._resolve_key_generic(
                "gemini-3.1-flash-lite",
                mock_get_key,
                [],
            )
            for _ in range(10)
        ]
        results = await asyncio.gather(*tasks)

        # All should return a key (no None)
        for key, model, err in results:
            assert key is not None
            assert "api_key" in key
            assert err is None

        # All 10 calls should have completed
        assert call_count == 10


# ============================================================================
# 5. ERROR PIPELINE CONCURRENT CLASSIFICATION
# ============================================================================


class TestErrorPipelineConcurrent:
    """Verify error classification functions are thread-safe under concurrent access."""

    @pytest.mark.asyncio
    async def test_concurrent_error_classification(self):
        """is_error_message and is_retryable_error should work correctly under concurrent calls."""
        from app.errors import (
            ErrorCode,
            is_error_message,
            is_retryable_error,
            tag_error,
        )

        tagged_errors = [tag_error(ErrorCode.RATE_LIMIT, f"Rate limit {i}") for i in range(50)]
        normal_texts = [f"Normal response {i}" for i in range(50)]

        async def classify_error(text):
            return is_error_message(text), is_retryable_error(text)

        # Classify all 100 texts concurrently
        all_texts = tagged_errors + normal_texts
        results = await asyncio.gather(*[classify_error(t) for t in all_texts])

        # First 50 should be errors + retryable
        for is_err, is_retry in results[:50]:
            assert is_err is True
            assert is_retry is True

        # Last 50 should NOT be errors
        for is_err, is_retry in results[50:]:
            assert is_err is False

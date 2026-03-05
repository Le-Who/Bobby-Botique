"""Tests for app.resilience_policy — retry delays, transient error detection, run_with_resilience."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.resilience_policy import ResiliencePolicy, is_transient_error, run_with_resilience


# ═══════════════════════════════════════════════════════════════════════════════
# ResiliencePolicy.compute_delay
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeDelay:
    def test_first_attempt_near_base_delay(self):
        policy = ResiliencePolicy(base_delay_s=1.0, jitter_s=0.0)
        delay = policy.compute_delay(0)
        assert delay == pytest.approx(1.0)

    def test_exponential_backoff(self):
        policy = ResiliencePolicy(base_delay_s=1.0, max_delay_s=100.0, jitter_s=0.0)
        d0 = policy.compute_delay(0)
        d1 = policy.compute_delay(1)
        d2 = policy.compute_delay(2)
        assert d1 > d0
        assert d2 > d1

    def test_max_delay_caps_exponential(self):
        policy = ResiliencePolicy(base_delay_s=1.0, max_delay_s=5.0, jitter_s=0.0)
        delay = policy.compute_delay(10)  # Would be 1024 without cap
        assert delay <= 5.0

    def test_jitter_adds_randomness(self):
        policy = ResiliencePolicy(base_delay_s=1.0, jitter_s=1.0)
        delays = {policy.compute_delay(0) for _ in range(20)}
        # With jitter, not all delays should be identical
        assert len(delays) > 1


# ═══════════════════════════════════════════════════════════════════════════════
# is_transient_error
# ═══════════════════════════════════════════════════════════════════════════════


class TestIsTransientError:
    @pytest.mark.parametrize(
        "text",
        [
            "503 Service Unavailable",
            "Service temporarily unavailable",
            "Model overloaded",
            "Rate limit exceeded",
            "Connection timeout",
            "Timeout waiting for response",
            "Connection refused",
            "Temporarily down",
        ],
    )
    def test_transient_errors_detected(self, text):
        assert is_transient_error(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Invalid API key",
            "Permission denied",
            "Bad request",
            "Not found",
            "Validation error",
        ],
    )
    def test_permanent_errors_not_retried(self, text):
        assert is_transient_error(text) is False

    def test_case_insensitive(self):
        assert is_transient_error("SERVICE UNAVAILABLE") is True
        assert is_transient_error("TIMEOUT") is True


# ═══════════════════════════════════════════════════════════════════════════════
# run_with_resilience
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunWithResilience:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        op = AsyncMock(return_value="OK")
        result, attempts = await run_with_resilience(op)
        assert result == "OK"
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        op = AsyncMock(side_effect=[ConnectionError("timeout"), "OK"])
        policy = ResiliencePolicy(max_retries=3, base_delay_s=0.01, jitter_s=0.0)
        result, attempts = await run_with_resilience(
            op,
            policy=policy,
            is_retryable=lambda e: True,
        )
        assert result == "OK"
        assert attempts == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        op = AsyncMock(side_effect=ConnectionError("always fails"))
        policy = ResiliencePolicy(max_retries=2, base_delay_s=0.01, jitter_s=0.0)
        with pytest.raises(ConnectionError, match="always fails"):
            await run_with_resilience(
                op,
                policy=policy,
                is_retryable=lambda e: True,
            )
        assert op.await_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_non_retryable(self):
        op = AsyncMock(side_effect=ValueError("permanent"))
        policy = ResiliencePolicy(max_retries=3, base_delay_s=0.01)
        with pytest.raises(ValueError, match="permanent"):
            await run_with_resilience(
                op,
                policy=policy,
                is_retryable=lambda e: False,
            )
        assert op.await_count == 1  # No retry

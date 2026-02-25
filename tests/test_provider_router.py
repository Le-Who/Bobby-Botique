"""
Tests for ProviderRouter and KeyHealth.
"""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai_provider import KeyHealth, ProviderRouter


class TestKeyHealth:
    """Tests for KeyHealth scoring mechanics."""

    def test_initial_state(self):
        h = KeyHealth(key_hash="abc123")
        assert h.score == 1.0
        assert h.consecutive_failures == 0
        assert h.is_healthy is True

    def test_single_failure_reduces_score(self):
        h = KeyHealth(key_hash="abc123")
        h.record_failure()
        assert h.score < 1.0
        assert h.consecutive_failures == 1
        assert h.total_failures == 1

    def test_multiple_failures_decay_exponentially(self):
        h = KeyHealth(key_hash="abc123")
        scores = [h.score]
        for _ in range(5):
            h.record_failure()
            scores.append(h.score)

        # Each score should be strictly less than the previous
        for i in range(1, len(scores)):
            assert scores[i] < scores[i - 1], f"Score should decrease: {scores}"

        # After 5 failures, score should be very low
        assert h.score < 0.1

    def test_success_recovers_score(self):
        h = KeyHealth(key_hash="abc123")
        h.record_failure()
        h.record_failure()
        low_score = h.score

        h.record_success()
        assert h.score > low_score
        assert h.consecutive_failures == 0
        assert h.total_successes == 1

    def test_score_capped_at_one(self):
        h = KeyHealth(key_hash="abc123")
        for _ in range(100):
            h.record_success()
        assert h.score == 1.0

    def test_score_floored_at_zero(self):
        h = KeyHealth(key_hash="abc123")
        for _ in range(100):
            h.record_failure()
        assert h.score >= 0.0

    def test_unhealthy_key_with_low_score(self):
        h = KeyHealth(key_hash="abc123")
        # Force score below threshold
        for _ in range(10):
            h.record_failure()
        assert h.score < 0.3

        # Override last_failure_time to be recent
        h.last_failure_time = time.monotonic()
        assert h.is_healthy is False

    def test_unhealthy_key_recovers_after_cooldown(self):
        h = KeyHealth(key_hash="abc123")
        for _ in range(10):
            h.record_failure()

        # Simulate cooldown elapsed
        h.last_failure_time = time.monotonic() - (h._COOLDOWN_SECONDS + 1)
        assert h.is_healthy is True


class TestProviderRouter:
    """Tests for ProviderRouter.get_response."""

    @pytest.mark.asyncio
    async def test_successful_response(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=({"api_key": "key1", "key_hash": "hash1"}, "gemini-2.0-flash", None)
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("Hello!", 10))
        mock_use_case.increment_key_usage = AsyncMock()

        with patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case):
            text, tokens = await router.get_response(
                "gemini-2.0-flash",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert text == "Hello!"
        assert tokens == 10
        # Health should be recorded as success
        health = router._get_health("hash1")
        assert health.total_successes == 1

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(None, None, "all_exhausted")
        )

        with patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case):
            text, tokens = await router.get_response(
                "gemini-2.0-flash",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_key_failure_triggers_retry(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()

        # First call returns a key that produces an error, second call succeeds
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-2.0-flash", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-2.0-flash", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("❌ API key invalid", None),  # First key fails
                ("Hello!", 10),  # Second key succeeds
            ]
        )
        mock_use_case.increment_key_usage = AsyncMock()

        with patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case):
            text, tokens = await router.get_response(
                "gemini-2.0-flash",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        assert text == "Hello!"
        assert tokens == 10
        # First key should have a failure, second should have a success
        assert router._get_health("hash1").total_failures == 1
        assert router._get_health("hash2").total_successes == 1

    @pytest.mark.asyncio
    async def test_unhealthy_key_skipped(self):
        router = ProviderRouter()

        # Pre-damage a key's health
        health = router._get_health("hash1")
        for _ in range(10):
            health.record_failure()
        health.last_failure_time = time.monotonic()  # recent failure

        mock_use_case = MagicMock()
        # First resolve returns the unhealthy key, second returns a healthy one
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                ({"api_key": "key1", "key_hash": "hash1"}, "gemini-2.0-flash", None),
                ({"api_key": "key2", "key_hash": "hash2"}, "gemini-2.0-flash", None),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("OK", 5))
        mock_use_case.increment_key_usage = AsyncMock()

        with patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case):
            text, tokens = await router.get_response(
                "gemini-2.0-flash",
                [{"role": "user", "parts": ["hi"]}],
                max_key_retries=3,
            )

        assert text == "OK"
        # The router should have called resolve twice (skipped unhealthy key1)
        assert mock_use_case.resolve_ai_request.call_count == 2

    @pytest.mark.asyncio
    async def test_openrouter_detection(self):
        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(None, None, "all_exhausted")
        )

        with patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case):
            text, _ = await router.get_response(
                "openai/gpt-4o",
                [{"role": "user", "parts": ["hi"]}],
            )

        assert "OpenRouter" in text

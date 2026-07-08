"""
Integration tests for the ProviderRouter — full chain from router → provider → response.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestProviderRouterIntegration:
    """Tests the full ProviderRouter chain including key resolution, dispatch, and health tracking."""

    @pytest.mark.asyncio
    async def test_gemini_full_chain_success(self):
        """Router resolves key → calls get_ai_response → returns text+tokens."""
        from app.providers import ProviderRouter

        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(
                {"key_hash": "abc", "api_key": "test-key"},
                "gemini-3.1-flash-lite",
                "resolved",
            )
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("Hello!", 42))
        mock_use_case.increment_key_usage = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch.object(
                router._rate_limiter,
                "check_rate_limit",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            text, tokens = await router.get_response(
                preferred_model="gemini-3.1-flash-lite",
                history=[{"role": "user", "parts": ["hi"]}],
                user_id=123,
            )

        assert text == "Hello!"
        assert tokens == 42
        mock_use_case.increment_key_usage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_openrouter_full_chain_success(self):
        """Router correctly handles OpenRouter slash-model."""
        from app.providers import ProviderRouter

        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(
                {"key_hash": "xyz", "api_key": "or-key"},
                "openai/gpt-4o",
                "resolved",
            )
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("GPT says hi", 37))
        mock_use_case.increment_key_usage = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch.object(
                router._rate_limiter,
                "check_rate_limit",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            text, tokens = await router.get_response(
                preferred_model="openai/gpt-4o",
                history=[{"role": "user", "parts": ["hello"]}],
                user_id=42,
                use_openrouter=True,
            )

        assert text == "GPT says hi"
        assert tokens == 37

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self):
        """Router returns error when resolve_ai_request reports all_exhausted."""
        from app.providers import ProviderRouter

        router = ProviderRouter()

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(
                None,
                None,
                "all_exhausted",
            )
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch.object(
                router._rate_limiter,
                "check_rate_limit",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            text, tokens = await router.get_response(
                preferred_model="gemini-3.1-flash-lite",
                history=[{"role": "user", "parts": ["hi"]}],
                user_id=1,
            )

        assert "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_rate_limited_user(self):
        """Router blocks user when rate limit is exceeded."""
        from app.providers import ProviderRouter

        router = ProviderRouter()

        with patch.object(
            router._rate_limiter,
            "check_rate_limit",
            new_callable=AsyncMock,
            return_value=False,
        ):
            text, tokens = await router.get_response(
                preferred_model="gemini-3.1-flash-lite",
                history=[{"role": "user", "parts": ["hi"]}],
                user_id=123,
            )

        assert "⏳" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_multimodal_forces_gemini(self):
        """When history contains images and use_openrouter=None, force Gemini."""
        from PIL import Image

        from app.providers import ProviderRouter

        router = ProviderRouter()

        # Create a tiny image for multimodal detection
        img = Image.new("RGB", (1, 1))
        history = [{"role": "user", "parts": ["describe this", img]}]

        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            return_value=(
                {"key_hash": "abc", "api_key": "test-key"},
                "gemini-3.1-flash-lite",
                "resolved",
            )
        )
        mock_use_case.get_ai_response = AsyncMock(return_value=("Image desc", 50))
        mock_use_case.increment_key_usage = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch.object(
                router._rate_limiter,
                "check_rate_limit",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            text, tokens = await router.get_response(
                preferred_model="gemini-3.1-flash-lite",
                history=history,
                user_id=1,
            )

        assert text == "Image desc"
        # resolve_ai_request should have been called with use_openrouter=False
        call_kwargs = mock_use_case.resolve_ai_request.call_args
        assert call_kwargs[1].get("use_openrouter") is False or call_kwargs[0][0] == "gemini-3.1-flash-lite"

    @pytest.mark.asyncio
    async def test_key_failure_triggers_retry(self):
        """Router retries with a different key when the first key returns a key-related error."""
        from app.providers import ProviderRouter

        router = ProviderRouter()

        # First call: returns error → retry; second call: succeeds
        mock_use_case = MagicMock()
        mock_use_case.resolve_ai_request = AsyncMock(
            side_effect=[
                (
                    {"key_hash": "bad_key", "api_key": "bad"},
                    "gemini-3.1-flash-lite",
                    "resolved",
                ),
                (
                    {"key_hash": "good_key", "api_key": "good"},
                    "gemini-3.1-flash-lite",
                    "resolved",
                ),
            ]
        )
        mock_use_case.get_ai_response = AsyncMock(
            side_effect=[
                ("❌ API key is invalid. Please check your API key.", None),
                ("Success!", 55),
            ]
        )
        mock_use_case.increment_key_usage = AsyncMock()

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=mock_use_case),
            patch.object(
                router._rate_limiter,
                "check_rate_limit",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            text, tokens = await router.get_response(
                preferred_model="gemini-3.1-flash-lite",
                history=[{"role": "user", "parts": ["hi"]}],
                user_id=1,
            )

        assert text == "Success!"
        assert tokens == 55
        assert mock_use_case.resolve_ai_request.call_count == 2

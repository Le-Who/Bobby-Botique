"""
Tests for the unified AI call path through AgentRequestUseCase.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers import AIResponse


class TestUnifiedCallPath:
    """AgentRequestUseCase.get_ai_response() should route through Provider classes."""

    @pytest.mark.asyncio
    async def test_gemini_routes_through_provider(self):
        """Non-slash model should create GeminiProvider and call get_response."""
        from app.agent_use_cases import AgentRequestUseCase

        use_case = AgentRequestUseCase()

        mock_response = AIResponse(
            text="Gemini says hi",
            token_count=42,
            success=True,
            provider="gemini",
            model="gemini-3.1-flash-lite-preview",
        )

        with patch("app.providers.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await use_case.get_ai_response(
                api_key="test-key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite-preview",
                use_openrouter=False,
            )

        assert text == "Gemini says hi"
        assert tokens == 42
        mock_factory.assert_called_once_with("gemini-3.1-flash-lite-preview", "test-key")

    @pytest.mark.asyncio
    async def test_openrouter_routes_through_provider(self):
        """Slash model should create OpenRouterProvider and call get_response."""
        from app.agent_use_cases import AgentRequestUseCase

        use_case = AgentRequestUseCase()

        mock_response = AIResponse(
            text="OpenRouter says hi",
            token_count=37,
            success=True,
            provider="openrouter",
            model="openai/gpt-4o",
        )

        with (
            patch("app.providers.get_provider_for_model") as mock_factory,
            patch("app.agent_use_cases.get_openrouter_keys", return_value=["key1"]),
        ):
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await use_case.get_ai_response(
                api_key="test-key",
                history=[{"role": "user", "parts": ["hello"]}],
                model_name="openai/gpt-4o",
                use_openrouter=True,
            )

        assert text == "OpenRouter says hi"
        assert tokens == 37
        mock_factory.assert_called_once_with("openai/gpt-4o", "test-key")

    @pytest.mark.asyncio
    async def test_error_response_returns_none_tokens(self):
        """On error, token_count should be None."""
        from app.agent_use_cases import AgentRequestUseCase

        use_case = AgentRequestUseCase()

        mock_response = AIResponse(
            text="❌ Error: API key invalid",
            token_count=0,
            success=False,
            error_message="API key invalid",
            provider="gemini",
            model="gemini-3.1-flash-lite-preview",
        )

        with patch("app.providers.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(return_value=mock_response)
            mock_factory.return_value = mock_provider

            text, tokens = await use_case.get_ai_response(
                api_key="bad-key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-3.1-flash-lite-preview",
            )

        assert "❌" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_openrouter_no_keys_returns_error(self):
        """Should return error message when OpenRouter has no keys configured."""
        from app.agent_use_cases import AgentRequestUseCase

        use_case = AgentRequestUseCase()

        with patch("app.agent_use_cases.get_openrouter_keys", return_value=[]):
            text, tokens = await use_case.get_ai_response(
                api_key="any-key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="openai/gpt-4o",
                use_openrouter=True,
            )

        assert "❌" in text
        assert tokens is None

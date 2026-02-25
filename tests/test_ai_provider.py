"""
Tests for the AI provider abstraction layer.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.ai_provider import (
    BaseAIProvider,
    AIResponse,
    GeminiProvider,
    OpenRouterProvider,
    is_openrouter_model,
    get_ai_response,
)


class TestAIResponse:
    """Tests for AIResponse dataclass."""

    def test_successful_response(self):
        response = AIResponse(
            text="Hello, world!",
            token_count=10,
            success=True,
            provider="gemini",
            model="gemini-2.0-flash",
        )
        assert response.text == "Hello, world!"
        assert response.token_count == 10
        assert response.success is True
        assert response.is_error is False
        assert response.error_message is None

    def test_error_response(self):
        response = AIResponse(
            text="❌ Error occurred",
            token_count=0,
            success=False,
            error_message="Something went wrong",
            provider="openrouter",
            model="openai/gpt-4o",
        )
        assert response.success is False
        assert response.is_error is True
        assert response.error_message == "Something went wrong"

    def test_is_error_with_error_message(self):
        """is_error should be True if error_message is set, even if success=True"""
        response = AIResponse(
            text="text", token_count=5, success=True, error_message="warning"
        )
        assert response.is_error is True


class TestIsOpenRouterModel:
    """Tests for is_openrouter_model function."""

    def test_openrouter_model(self):
        """OpenRouter models contain a slash."""
        assert is_openrouter_model("openai/gpt-4o") is True
        assert is_openrouter_model("anthropic/claude-3-opus") is True
        assert is_openrouter_model("meta-llama/llama-3-70b") is True

    def test_gemini_model(self):
        """Gemini models don't contain a slash."""
        assert is_openrouter_model("gemini-2.0-flash") is False
        assert is_openrouter_model("gemini-1.5-pro") is False
        assert is_openrouter_model("gemini-pro-vision") is False


class TestBaseAIProvider:
    """Tests for BaseAIProvider abstract class."""

    def test_init_with_valid_key(self):
        """Should initialize with valid API key."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        provider = TestProvider("valid-key")
        assert provider.api_key == "valid-key"

    def test_init_with_invalid_key(self):
        """Should raise ValueError for invalid API key."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        with pytest.raises(ValueError, match="api_key must be a non-empty string"):
            TestProvider("")

        with pytest.raises(ValueError, match="api_key must be a non-empty string"):
            TestProvider("   ")

    def test_validate_inputs_empty_history(self):
        """Should raise ValueError for empty history."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        provider = TestProvider("key")
        with pytest.raises(ValueError, match="history must be a non-empty list"):
            provider._validate_inputs([], "model", None, None)

    def test_validate_inputs_invalid_model(self):
        """Should raise ValueError for invalid model name."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        provider = TestProvider("key")
        with pytest.raises(ValueError, match="model_name must be a non-empty string"):
            provider._validate_inputs(
                [{"role": "user", "parts": ["hi"]}], "", None, None
            )

    def test_is_transient_error(self):
        """Should correctly identify transient errors."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        provider = TestProvider("key")

        # Transient errors
        assert provider._is_transient_error("503 Service Unavailable") is True
        assert provider._is_transient_error("Server overloaded") is True
        assert provider._is_transient_error("Rate limit exceeded") is True
        assert provider._is_transient_error("Connection timeout") is True

        # Non-transient errors
        assert provider._is_transient_error("Invalid API key") is False
        assert provider._is_transient_error("Quota exceeded") is False

    def test_categorize_error(self):
        """Should return appropriate user-facing messages."""

        class TestProvider(BaseAIProvider):
            provider_name = "test"

            async def _execute_request(self, **kwargs):
                pass

        provider = TestProvider("key")

        assert "лимит запросов" in provider._categorize_error(
            Exception("quota exceeded")
        )
        assert "перегружен" in provider._categorize_error(Exception("503 unavailable"))
        assert "ключ" in provider._categorize_error(Exception("401 unauthorized"))


class TestProviders:
    """Tests for provider classes."""

    @pytest.mark.asyncio
    async def test_gemini_wrapper_success(self):
        """GeminiProvider should return AIResponse on success."""
        wrapper = GeminiProvider("test-key")

        mock_response = MagicMock()
        mock_response.text = "Hello!"
        mock_token = MagicMock()
        mock_token.total_tokens = 15

        with (
            patch("app.ai_provider.genai.Client") as MockClient,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
            patch("app.ai_provider.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []
            mock_aio = MagicMock()
            mock_aio.generate_content = AsyncMock(return_value=mock_response)
            mock_aio.count_tokens = AsyncMock(return_value=mock_token)
            MockClient.return_value.aio.models = mock_aio

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.0-flash",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.text == "Hello!"
            assert response.token_count == 15
            assert response.success is True
            assert response.provider == "gemini"

    @pytest.mark.asyncio
    async def test_gemini_wrapper_error(self):
        """GeminiProvider should detect error responses."""
        wrapper = GeminiProvider("test-key")

        mock_response = MagicMock()
        mock_response.text = None  # Empty response triggers error

        with (
            patch("app.ai_provider.genai.Client") as MockClient,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
            patch("app.ai_provider.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []
            mock_aio = MagicMock()
            mock_aio.generate_content = AsyncMock(return_value=mock_response)
            mock_token = MagicMock()
            mock_token.total_tokens = 0
            mock_aio.count_tokens = AsyncMock(return_value=mock_token)
            MockClient.return_value.aio.models = mock_aio

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.0-flash",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.success is False
            assert response.is_error is True

    @pytest.mark.asyncio
    async def test_openrouter_wrapper_success(self):
        """OpenRouterProvider should return AIResponse on success."""
        wrapper = OpenRouterProvider("test-key")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Greetings!"}}],
            "usage": {"total_tokens": 20},
        }
        mock_resp.raise_for_status = MagicMock()

        with (
            patch("app.ai_provider._openrouter_http_client") as mock_client,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
        ):
            mock_client.post = AsyncMock(return_value=mock_resp)

            response = await wrapper._execute_request(
                history=[{"role": "user", "parts": ["hello"]}],
                model_name="openai/gpt-4o",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=120.0,
            )

            assert response.text == "Greetings!"
            assert response.token_count == 20
            assert response.success is True
            assert response.provider == "openrouter"


class TestGetAIResponse:
    """Tests for unified get_ai_response function."""

    @pytest.mark.asyncio
    async def test_delegates_to_gemini(self):
        """Should use Gemini for non-slash models."""
        with patch("app.ai_provider.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(
                return_value=AIResponse(
                    text="Gemini response", token_count=10, success=True
                )
            )
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.0-flash",
            )

            assert text == "Gemini response"
            assert tokens == 10

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self):
        """Should return None for token_count on error."""
        with patch("app.ai_provider.get_provider_for_model") as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_response = AsyncMock(
                return_value=AIResponse(text="❌ Error", token_count=0, success=False)
            )
            mock_factory.return_value = mock_provider

            text, tokens = await get_ai_response(
                api_key="key",
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="openai/gpt-4o",
            )

            assert text == "❌ Error"
            assert tokens is None

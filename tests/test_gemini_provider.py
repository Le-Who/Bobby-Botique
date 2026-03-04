"""
Tests for GeminiProvider._execute_request via the Provider class directly.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai.errors import APIError

from app.ai_provider import GeminiProvider


@pytest.mark.asyncio
async def test_execute_gemini_request_success():
    """Test GeminiProvider._execute_request happy path."""
    provider = GeminiProvider("key")

    with (
        patch("app.ai_provider.genai.Client") as MockClient,
        patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
        patch("app.ai_provider.api_logger", new_callable=MagicMock),
        patch("app.ai_provider.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        mock_client_instance = MockClient.return_value
        mock_response = MagicMock()
        mock_response.text = "Generated Text"
        # Token count from usage_metadata (not separate count_tokens call)
        mock_usage = MagicMock()
        mock_usage.total_token_count = 50
        mock_response.usage_metadata = mock_usage

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(return_value=mock_response)
        mock_client_instance.aio.models = mock_aio_models

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.text == "Generated Text"
        assert resp.token_count == 50
        assert resp.success is True


@pytest.mark.asyncio
async def test_execute_gemini_request_503_error():
    """Test GeminiProvider._execute_request with 503 error raises for retry."""
    provider = GeminiProvider("key")

    with (
        patch("app.ai_provider.genai.Client") as MockClient,
        patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
        patch("app.ai_provider.api_logger", new_callable=MagicMock),
        patch("app.ai_provider.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        mock_client_instance = MockClient.return_value
        mock_response_obj = MagicMock()
        mock_response_obj.status_code = 503
        mock_response_obj.text = "Service Unavailable"
        error = APIError(503, mock_response_obj)

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(side_effect=error)
        mock_client_instance.aio.models = mock_aio_models

        with pytest.raises(APIError, match="503"):
            await provider._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="model",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=100.0,
            )


@pytest.mark.asyncio
async def test_execute_gemini_request_other_error():
    """Test GeminiProvider._execute_request with non-retryable error."""
    provider = GeminiProvider("key")

    with (
        patch("app.ai_provider.genai.Client") as MockClient,
        patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
        patch("app.ai_provider.api_logger", new_callable=MagicMock),
        patch("app.ai_provider.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        mock_client_instance = MockClient.return_value
        mock_response_obj = MagicMock()
        mock_response_obj.status_code = 400
        mock_response_obj.text = "Bad Request"
        error = APIError(400, mock_response_obj)

        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(side_effect=error)
        mock_client_instance.aio.models = mock_aio_models

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.success is False
        assert "ошибка" in resp.text.lower() or "Bad Request" in resp.text


@pytest.mark.asyncio
async def test_execute_gemini_request_timeout():
    """Test GeminiProvider._execute_request timeout."""
    provider = GeminiProvider("key")

    def timeout_side_effect(coro, timeout=None):
        coro.close()
        raise TimeoutError("Timeout")

    with (
        patch("app.ai_provider.genai.Client") as _MockClient,
        patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
        patch("app.ai_provider.api_logger", new_callable=MagicMock),
        patch("app.ai_provider.settings") as mock_settings,
        patch("asyncio.wait_for", side_effect=timeout_side_effect),
    ):
        mock_settings.SAFETY_SETTINGS = []

        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="model",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=100.0,
        )

        assert resp.success is False
        assert "Превышено время ожидания" in resp.text

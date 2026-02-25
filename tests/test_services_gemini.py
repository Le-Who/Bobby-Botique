import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from app.services import get_gemini_response, _execute_gemini_request
from google.genai.errors import APIError


@pytest.mark.asyncio
async def test_get_gemini_response_input_validation():
    """Test input validation for get_gemini_response."""

    # Empty API key
    with pytest.raises(ValueError, match="api_key must be a non-empty string"):
        await get_gemini_response("", [], "model")

    # Empty history
    with pytest.raises(ValueError, match="history must be a non-empty list"):
        await get_gemini_response("key", [], "model")

    # Empty model_name
    with pytest.raises(ValueError, match="model_name must be a non-empty string"):
        await get_gemini_response("key", [{"role": "user", "parts": ["hi"]}], "")

    # Invalid user_id
    with pytest.raises(ValueError, match="user_id must be an integer"):
        await get_gemini_response(
            "key", [{"role": "user", "parts": ["hi"]}], "model", user_id="invalid"
        )

    # Invalid chat_id
    with pytest.raises(ValueError, match="chat_id must be an integer"):
        await get_gemini_response(
            "key", [{"role": "user", "parts": ["hi"]}], "model", chat_id="invalid"
        )


@pytest.mark.asyncio
async def test_get_gemini_response_retry_success():
    """Test successful retry logic in get_gemini_response."""
    with patch(
        "app.services._execute_gemini_request", new_callable=AsyncMock
    ) as mock_exec:
        # First call fails with 503 (simulated by raising Exception), second succeeds
        mock_exec.side_effect = [
            Exception("503 Service Unavailable"),
            ("Success Response", 100),
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            response, tokens = await get_gemini_response(
                "key", [{"role": "user", "parts": ["hi"]}], "model", max_retries=3
            )

            assert response == "Success Response"
            assert tokens == 100
            assert mock_exec.call_count == 2
            mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_get_gemini_response_retry_exhausted():
    """Test exhausted retries in get_gemini_response."""
    with patch(
        "app.services._execute_gemini_request", new_callable=AsyncMock
    ) as mock_exec:
        # Always fails with 503
        mock_exec.side_effect = Exception("503 Service Unavailable")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            response, tokens = await get_gemini_response(
                "key", [{"role": "user", "parts": ["hi"]}], "model", max_retries=3
            )

            # Should return the fallback error message
            assert "Превышено максимальное количество попыток" in response
            assert tokens is None
            assert mock_exec.call_count == 3
            assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_get_gemini_response_non_retryable_error():
    """Test non-retryable error in get_gemini_response."""
    with patch(
        "app.services._execute_gemini_request", new_callable=AsyncMock
    ) as mock_exec:
        # Fails with non-503 error
        mock_exec.side_effect = Exception("400 Bad Request")

        response, tokens = await get_gemini_response(
            "key", [{"role": "user", "parts": ["hi"]}], "model"
        )

        assert "400 Bad Request" in response
        assert tokens is None
        assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_execute_gemini_request_success():
    """Test _execute_gemini_request happy path."""
    with (
        patch("app.services.genai.Client") as MockClient,
        patch("app.services.metrics_collector", new_callable=AsyncMock),
        patch("app.services.api_logger", new_callable=MagicMock),
        patch("app.services.settings") as mock_settings,
    ):
        mock_settings.SAFETY_SETTINGS = []

        mock_client_instance = MockClient.return_value
        mock_response = MagicMock()
        mock_response.text = "Generated Text"
        mock_token_count = MagicMock()
        mock_token_count.total_tokens = 50

        # Mock async generate_content and count_tokens (client.aio.models.*)
        mock_aio_models = MagicMock()
        mock_aio_models.generate_content = AsyncMock(return_value=mock_response)
        mock_aio_models.count_tokens = AsyncMock(return_value=mock_token_count)
        mock_client_instance.aio.models = mock_aio_models

        response, tokens = await _execute_gemini_request(
            "key", [{"role": "user", "parts": ["hi"]}], "model"
        )

        assert response == "Generated Text"
        assert tokens == 50


@pytest.mark.asyncio
async def test_execute_gemini_request_503_error():
    """Test _execute_gemini_request with 503 error."""
    with (
        patch("app.services.genai.Client") as MockClient,
        patch("app.services.metrics_collector", new_callable=AsyncMock),
        patch("app.services.api_logger", new_callable=MagicMock),
        patch("app.services.settings") as mock_settings,
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

        # Currently, this returns a string. After fix, it should raise APIError or similar.
        # We assert the current behavior (it returns string), but mark as something expected to change if we want.
        # However, plan step 2 says "Confirm that the retry logic tests fail as expected".
        # This specific test checks _execute_gemini_request directly.

        # If we expect the FIX to make this RAISE, we should assert checking for raise.
        # But currently it returns a string.
        # So verifying "current behavior is broken/undesirable" -> returns string.
        # Verifying "future behavior" -> raises.

        # Let's write the test expecting the FIX behavior (raise), so it fails now.
        with pytest.raises(APIError, match="503"):
            await _execute_gemini_request(
                "key", [{"role": "user", "parts": ["hi"]}], "model"
            )


@pytest.mark.asyncio
async def test_execute_gemini_request_other_error():
    """Test _execute_gemini_request with other error."""
    with (
        patch("app.services.genai.Client") as MockClient,
        patch("app.services.metrics_collector", new_callable=AsyncMock),
        patch("app.services.api_logger", new_callable=MagicMock),
        patch("app.services.settings") as mock_settings,
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

        response, tokens = await _execute_gemini_request(
            "key", [{"role": "user", "parts": ["hi"]}], "model"
        )

        assert "Произошла ошибка вызова API" in response or "Bad Request" in response
        assert tokens is None


@pytest.mark.asyncio
async def test_execute_gemini_request_timeout():
    """Test _execute_gemini_request timeout."""

    def timeout_side_effect(coro, timeout=None):
        coro.close()
        raise asyncio.TimeoutError("Timeout")

    with (
        patch("app.services.genai.Client") as MockClient,
        patch("app.services.metrics_collector", new_callable=AsyncMock),
        patch("app.services.api_logger", new_callable=MagicMock),
        patch("app.services.settings") as mock_settings,
        patch("asyncio.wait_for", side_effect=timeout_side_effect),
    ):
        mock_settings.SAFETY_SETTINGS = []

        response, tokens = await _execute_gemini_request(
            "key", [{"role": "user", "parts": ["hi"]}], "model"
        )

        assert "Превышено время ожидания" in response
        assert tokens is None

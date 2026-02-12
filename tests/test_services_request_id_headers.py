import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.request_context import clear_request_id, set_request_id
from app.services import _execute_openrouter_request, _tavily_api_call


@pytest.mark.asyncio
async def test_tavily_api_call_adds_request_id_header():
    set_request_id("req-123")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch("app.services.http_client.post", new=AsyncMock(return_value=mock_response)) as mock_post:
        result = await _tavily_api_call({"query": "hello"})

    assert result == {"ok": True}
    assert mock_post.await_count == 1
    assert mock_post.await_args.kwargs["headers"]["X-Request-ID"] == "req-123"

    clear_request_id()


@pytest.mark.asyncio
async def test_openrouter_request_adds_request_id_header():
    set_request_id("req-openrouter-456")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 7},
    }

    with patch("app.services.http_client.post", new=AsyncMock(return_value=mock_response)) as mock_post, \
         patch("app.services.metrics_collector.record_api_call", new=AsyncMock()), \
         patch("app.services.metrics_collector.record_error", new=AsyncMock()), \
         patch("app.services.api_logger.log_gemini_response", new=MagicMock()):
        text, tokens = await _execute_openrouter_request(
            api_key="test-key",
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="openai/gpt-4o-mini",
        )

    assert text == "ok"
    assert tokens == 7
    assert mock_post.await_count == 1
    assert mock_post.await_args.kwargs["headers"]["X-Request-ID"] == "req-openrouter-456"

    clear_request_id()

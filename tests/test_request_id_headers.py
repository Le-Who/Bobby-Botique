from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai_provider import OpenRouterProvider
from app.request_context import clear_request_id, set_request_id
from app.search_services import _tavily_api_call


@pytest.mark.asyncio
async def test_tavily_api_call_adds_request_id_header():
    set_request_id("req-123")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"ok": True}

    with patch(
        "app.search_services.http_client.post", new=AsyncMock(return_value=mock_response)
    ) as mock_post:
        result = await _tavily_api_call({"query": "hello"})

    assert result == {"ok": True}
    assert mock_post.await_count == 1
    assert mock_post.await_args.kwargs["headers"]["X-Request-ID"] == "req-123"

    clear_request_id()


@pytest.mark.asyncio
async def test_openrouter_request_adds_request_id_header():
    set_request_id("req-openrouter-456")

    provider = OpenRouterProvider("test-key")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 7},
    }

    with (
        patch(
            "app.ai_provider._openrouter_http_client.post", new=AsyncMock(return_value=mock_response)
        ) as mock_post,
        patch("app.ai_provider.metrics_collector.record_api_call", new=AsyncMock()),
        patch("app.ai_provider.metrics_collector.record_error", new=AsyncMock()),
        patch("app.ai_provider.api_logger.log_gemini_response", new=MagicMock()),
    ):
        resp = await provider._execute_request(
            history=[{"role": "user", "parts": ["hi"]}],
            model_name="openai/gpt-4o-mini",
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=90.0,
        )

    assert resp.text == "ok"
    assert resp.token_count == 7
    assert mock_post.await_count == 1
    assert (
        mock_post.await_args.kwargs["headers"]["X-Request-ID"] == "req-openrouter-456"
    )

    clear_request_id()

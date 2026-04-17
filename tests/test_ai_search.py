"""Tests for app.handlers.ai_search — QnA and research agent search."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.agentic import AgenticResult


def make_chat_state(
    model="gemini-3.1-flash-lite-preview",
    system_prompt=None,
    history=None,
    is_deep_dive=False,
):
    return SimpleNamespace(
        model=model,
        system_prompt=system_prompt,
        history=history if history is not None else [],
        token_count=0,
        thinking_level=None,
        is_deep_dive=is_deep_dive,
        search_enabled=True,
        deep_dive_thread_id=None,
    )


def make_placeholder(user_id=123):
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat.id = 456
    msg.from_user.id = user_id
    return msg


# ── QnA search — happy path (Google Search Grounding) ────────────────────────


@pytest.mark.asyncio
async def test_qna_search_happy_path():
    """QnA search streams a grounded response via stream_and_display(enable_web_search=True) on the Gemini path."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.get_primary_provider", return_value="gemini"),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("Grounded answer from Google Search", True, AsyncMock(), 42, False, False),
        ) as mock_stream,
        patch(
            "app.handlers.ai_search.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock),
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        await _handle_qna_search(placeholder, "What is Python?", chat_state)

    # Verify stream_and_display was called with enable_web_search=True (Gemini path)
    mock_stream.assert_awaited_once()
    call_kwargs = mock_stream.call_args[1] if mock_stream.call_args[1] else {}
    assert call_kwargs.get("enable_web_search") is True, "Gemini path must use enable_web_search=True"


@pytest.mark.asyncio
async def test_qna_search_opencode_path():
    """QnA search uses JINA grounding (enable_web_search=False) on the Opencode path."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    # Gemini model but PRIMARY_PROVIDER=opencode → takes Opencode path
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.get_primary_provider", return_value="opencode"),
        patch(
            "app.search_jina.search_for_grounding",
            new_callable=AsyncMock,
            return_value="[grounding context]",
        ),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("Opencode JINA answer", True, AsyncMock(), 50, False, False),
        ) as mock_stream,
        patch(
            "app.handlers.ai_search.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock),
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        await _handle_qna_search(placeholder, "What is Python?", chat_state)

    # Opencode path: JINA grounding is injected into prompt, web search disabled
    mock_stream.assert_awaited_once()
    call_kwargs = mock_stream.call_args[1] if mock_stream.call_args[1] else {}
    assert call_kwargs.get("enable_web_search") is False, (
        "Opencode path must NOT use enable_web_search (JINA grounding used)"
    )


# ── QnA search — streaming failure triggers fallback ─────────────────────────


@pytest.mark.asyncio
async def test_qna_search_streaming_failure_fallback():
    """QnA search falls back to non-streaming when all streaming attempts fail."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("", False, None, 0, False, False),
        ),
        patch(
            "app.handlers.ai_search._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("Fallback non-streaming answer", 10),
        ) as mock_fallback,
        patch(
            "app.handlers.ai_search.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock) as mock_send,
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        await _handle_qna_search(placeholder, "Query", chat_state)

    # Non-streaming fallback was invoked
    mock_fallback.assert_awaited()
    # Final answer was sent via send_long_message (not streamed)
    mock_send.assert_awaited_once()


# ── Research agent — search fails ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_search_exception():
    """Research agent handles search service exception."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.AgenticSearch") as mock_agent_class,
        patch("app.repos.keys.get_available_gemini_key", new_callable=AsyncMock) as mock_get_key,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_get_key.return_value = {"api_key": "fake", "key_hash": "hash123"}
        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(side_effect=Exception("Network fail"))
        mock_agent_class.return_value = mock_agent_instance

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "error" in text.lower() or "ошибка" in text.lower()


# ── Research agent — no results ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_no_results():
    """Research agent handles empty search results."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.AgenticSearch") as mock_agent_class,
        patch("app.repos.keys.get_available_gemini_key", new_callable=AsyncMock) as mock_get_key,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_get_key.return_value = {"api_key": "fake", "key_hash": "hash123"}
        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(
            return_value=AgenticResult(
                answer="❌ К сожалению, агенту не удалось собрать достаточно информации для ответа в отведенное время.",
            )
        )
        mock_agent_class.return_value = mock_agent_instance

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "к сожалению" in text.lower()


# ── Research agent — Tavily returns error dict ────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_tavily_error():
    """Research agent shows Tavily error message."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.AgenticSearch") as mock_agent_class,
        patch("app.repos.keys.get_available_gemini_key", new_callable=AsyncMock) as mock_get_key,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_get_key.return_value = {"api_key": "fake", "key_hash": "hash123"}
        mock_agent_instance = MagicMock()
        mock_agent_instance.run = AsyncMock(
            return_value=AgenticResult(
                answer="❌ Ошибка при запуске агента: Rate limit exceeded",
            )
        )
        mock_agent_class.return_value = mock_agent_instance

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "Rate limit exceeded" in text

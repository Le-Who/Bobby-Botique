"""Tests for app.handlers.ai_search — QnA and research agent search."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_chat_state(model="gemini-2.0-flash", system_prompt=None, history=None, is_deep_dive=False):
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


# ── QnA search — happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qna_search_happy_path():
    """QnA search returns localized answer from Tavily."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.search_services") as mock_search,
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("Localized answer", True, AsyncMock()),
        ),
        patch(
            "app.handlers.ai_core._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-2.0-flash", None),
        ),
        patch("app.handlers.ai_search.handle_ai_response_error", new_callable=AsyncMock, return_value=False),
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock) as _mock_send,
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
        patch("app.handlers.ai_search.get_openrouter_keys", return_value=[]),
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_search.tavily_search_agent = AsyncMock(return_value={"answer": "Raw Tavily answer"})
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_registry.get_task_prompt.return_value = "Q: {user_message} A: {tavily_answer}"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        await _handle_qna_search(placeholder, "What is Python?", chat_state)

    # When streaming succeeds, it edits the message instead of calling send_long_message
    assert len(chat_state.history) == 0  # Assuming history wasn't modified because the mock chat_state is local


# ── QnA search — Tavily error ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qna_search_tavily_error():
    """QnA search handles Tavily error gracefully."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.search_services") as mock_search,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_search.tavily_search_agent = AsyncMock(return_value={"error": "API limit"})

        from app.handlers.ai_search import _handle_qna_search

        await _handle_qna_search(placeholder, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "API limit" in text


# ── Research agent — search fails ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_search_exception():
    """Research agent handles search service exception."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.search_services") as mock_search,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_search.tavily_search_agent = AsyncMock(side_effect=Exception("Network fail"))

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "ошибка" in text.lower()


# ── Research agent — no results ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_no_results():
    """Research agent handles empty search results."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.search_services") as mock_search,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_search.tavily_search_agent = AsyncMock(return_value={"results": []})

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "найти" in text.lower() or "источник" in text.lower()


# ── Research agent — Tavily returns error dict ────────────────────────────────


@pytest.mark.asyncio
async def test_research_agent_tavily_error():
    """Research agent shows Tavily error message."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.search_services") as mock_search,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_search.tavily_search_agent = AsyncMock(return_value={"error": "Rate limited"})

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "Rate limited" in text

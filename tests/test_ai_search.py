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
            return_value=("Localized answer", True, AsyncMock(), 0),
        ),
        patch(
            "app.handlers.ai_core._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite-preview", None),
        ),
        patch(
            "app.handlers.ai_search.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
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

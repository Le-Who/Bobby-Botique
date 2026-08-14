"""Tests for app.handlers.ai_search — QnA and research agent search."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.agentic import AgenticResult
from app.errors import ErrorCode
from app.response_delivery.outcomes import CompleteDelivery, FailedDelivery
from app.response_delivery.renderer import (
    DeliveryKind,
    DeliveryReceipt,
    TelegramMessageRef,
)


def _receipt() -> DeliveryReceipt:
    return DeliveryReceipt(
        kind=DeliveryKind.MESSAGE,
        message_ids=(1,),
        final_message=TelegramMessageRef(chat_id=456, message_id=1),
    )


def make_chat_state(
    model="gemini-3.1-flash-lite",
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
    """QnA search uses typed delivery with provider-search grounding."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="Grounded answer from Google Search",
            displayed_text="Grounded answer from Google Search",
            completion=None,
            voice_requested=False,
            receipt=_receipt(),
        )
    )

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_search.get_primary_provider", return_value="gemini"),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        result = await _handle_qna_search(placeholder, "What is Python?", chat_state)

    assert result == "Grounded answer from Google Search"
    delivery.stream.assert_awaited_once()
    request = delivery.stream.await_args.args[1]
    assert request.grounding.value == "provider_search_required"
    presentation = delivery.stream.await_args.kwargs["presentation"]
    labels = [
        button.text
        for row in presentation.actions.inline_keyboard
        for button in row
    ]
    assert labels == ["🎭 Выбрать роль ИИ", "✨ Начать новую тему"]


@pytest.mark.asyncio
async def test_qna_search_opencode_path():
    """Opencode QnA carries JINA context through the typed request."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="Opencode JINA answer",
            displayed_text="Opencode JINA answer",
            completion=None,
            voice_requested=False,
            receipt=_receipt(),
        )
    )

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
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        result = await _handle_qna_search(placeholder, "What is Python?", chat_state)

    assert result == "Opencode JINA answer"
    request = delivery.stream.await_args.args[1]
    assert request.grounding.value == "provided_context"
    assert "[grounding context]" in (request.system_instruction or "")


@pytest.mark.asyncio
async def test_qna_search_typed_failure_does_not_duplicate_delivery():
    """Delivery owns the rendered failure; the handler must not send it again."""
    placeholder = make_placeholder()
    placeholder.get_bot.return_value = MagicMock()
    placeholder.chat.type = "private"
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=FailedDelivery(
            error_code=ErrorCode.KEYS_EXHAUSTED,
            displayed_text="No keys",
            receipt=_receipt(),
        )
    )

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.update_stage", new_callable=AsyncMock),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock) as mock_send,
        patch("app.handlers.ai_search.get_registry") as mock_get_registry,
    ):
        mock_metrics.record_search_query = AsyncMock()
        mock_registry = MagicMock()
        mock_registry.compose_system_prompt.return_value = "sys"
        mock_get_registry.return_value = mock_registry

        from app.handlers.ai_search import _handle_qna_search

        result = await _handle_qna_search(placeholder, "Query", chat_state)

    assert result is None
    delivery.stream.assert_awaited_once()
    mock_send.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_research_agent_uses_unified_completed_response_delivery():
    """AgenticSearch must not own a second Long Read implementation."""
    from app.response_delivery.delivery import CompletedResponse

    placeholder = make_placeholder()
    placeholder.message_id = 99
    placeholder.get_bot.return_value = None
    chat_state = make_chat_state()
    final_answer = "A" * 4200
    delivery = MagicMock()
    delivery.deliver = AsyncMock()

    with (
        patch("app.handlers.ai_search.metrics_collector") as mock_metrics,
        patch("app.handlers.ai_search.AgenticSearch") as agent_class,
        patch("app.handlers.ai_search._available_models", return_value=[]),
        patch("app.repos.keys.get_available_gemini_key", new_callable=AsyncMock) as get_key,
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.cache.store_long_message", new_callable=AsyncMock) as store_long,
        patch("app.handlers.ai_search.send_long_message", new_callable=AsyncMock) as send_long,
        patch("app.handlers.ai_search.update_user_chat", new_callable=AsyncMock),
    ):
        mock_metrics.record_search_query = AsyncMock()
        get_key.return_value = {"api_key": "fake", "key_hash": "hash123"}
        agent = MagicMock()
        agent.run = AsyncMock(return_value=AgenticResult(answer=final_answer))
        agent_class.return_value = agent

        from app.handlers.ai_search import _handle_research_agent

        await _handle_research_agent(placeholder, 123, "Query", chat_state)

    delivery.deliver.assert_awaited_once()
    completed = delivery.deliver.await_args.args[1]
    assert completed == CompletedResponse(final_answer)
    store_long.assert_not_awaited()
    send_long.assert_not_awaited()

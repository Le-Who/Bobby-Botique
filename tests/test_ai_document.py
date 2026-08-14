"""Tests for app.handlers.ai_document — document question handling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.config as config


@pytest.fixture(autouse=True)
def document_test_settings(monkeypatch) -> SimpleNamespace:
    settings = SimpleNamespace(
        ADMIN_ID=1,
        AVAILABLE_MODELS=["gemini-3.1-flash-lite"],
        DAILY_LIMITS={},
        DEFAULT_MODEL="gemini-3.1-flash-lite",
        GEMINI_API_KEYS=["fake-api-key-123"],
        LIMIT_THRESHOLD_PERCENT=0.7,
        QNA_MODEL="gemini-3.1-flash-lite",
        RESEARCH_MODEL="gemini-3.1-flash-lite",
        TAVILY_LIMIT_THRESHOLD_PERCENT=0.8,
        TAVILY_MONTHLY_CREDIT_LIMIT=1000.0,
        TELEGRAM_BOT_TOKEN="test-token",
    )
    monkeypatch.setattr(config, "settings", settings, raising=False)
    monkeypatch.setattr(config.config_manager, "_settings", settings, raising=False)
    return settings


def make_placeholder():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat.id = 456
    msg.from_user.id = 123
    return msg


def make_chat_state():
    return SimpleNamespace(
        model="gemini-3.1-flash-lite",
        system_prompt=None,
        history=[],
        token_count=0,
        is_deep_dive=False,
        search_enabled=False,
        thinking_level=None,
    )


# ── Happy path — AI answers question ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_success():
    """Successfully answers a question about an uploaded document."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()
    stream_message = MagicMock()
    stream_message.edit_reply_markup = AsyncMock()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "test.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value="Python is a programming language.",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("Python is great!", True, stream_message, 0, False, False),
        ) as mock_stream,
        patch(
            "app.handlers.ai_core._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite", None),
        ),
        patch(
            "app.handlers.ai_document._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("Python is great!", 10),
        ),
        patch(
            "app.handlers.ai_document.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_document.send_long_message", new_callable=AsyncMock) as _mock_send,
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "What is Python?", chat_state)

    reply_markup = mock_stream.await_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    labels = [button.text for row in reply_markup.inline_keyboard for button in row]
    assert labels == [
        "📄 Загрузить другой документ",
        "📋 Выбрать документ",
        "❌ Отменить работу с документами",
        "🎭 Выбрать роль ИИ",
        "✨ Начать новую тему",
    ]
    stream_message.edit_reply_markup.assert_not_awaited()


# ── No documents uploaded ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_no_documents():
    """Shows error when user has no uploaded documents."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "What is this?", chat_state)

    placeholder.edit_text.assert_awaited_once()
    text = placeholder.edit_text.call_args[0][0]
    assert "нет" in text.lower() and "документ" in text.lower()


# ── Document content retrieval fails ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_content_missing():
    """Handles case when document content cannot be retrieved."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "test.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    placeholder.edit_text.assert_awaited_once()
    text = placeholder.edit_text.call_args[0][0]
    assert "содержимое" in text.lower() or "не удалось" in text.lower()


# ── AI returns empty response ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_empty_ai_response():
    """Shows error when AI returns empty response."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "test.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value="Some document content",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("", False, AsyncMock(), 0, False, False),
        ),
        patch(
            "app.handlers.ai_core._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite", None),
        ),
        patch(
            "app.handlers.ai_document._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=(None, 0),
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "не удалось" in text.lower() or "ответ" in text.lower()


@pytest.mark.asyncio
async def test_document_question_fallback_reuses_resolved_stream_model():
    """Non-stream fallback must reuse the resolved model instead of resetting to DEFAULT_MODEL."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "test.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value="Some document content",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.streaming.stream_and_display",
            new_callable=AsyncMock,
            return_value=("", False, AsyncMock(), 0, False, False),
        ),
        patch(
            "app.handlers.ai_core._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "opencode-go/qwen3.5-plus", None),
        ),
        patch(
            "app.handlers.ai_document._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("Fallback answer", 10),
        ) as mock_fallback,
        patch(
            "app.handlers.ai_document.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_document.send_long_message", new_callable=AsyncMock),
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    assert mock_fallback.await_args.args[0] == "opencode-go/qwen3.5-plus"


# ── Exception during processing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_exception():
    """Handles general exception during document processing."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            side_effect=Exception("DB connection lost"),
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "ошибка" in text.lower()

"""Tests for app.handlers.ai_document — document question handling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_placeholder():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat.id = 456
    msg.from_user.id = 123
    return msg


def make_chat_state():
    return SimpleNamespace(
        model="gemini-2.0-flash",
        system_prompt=None,
        history=[],
        token_count=0,
        is_deep_dive=False,
        search_enabled=False,
    )


# ── Happy path — AI answers question ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_success():
    """Successfully answers a question about an uploaded document."""
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
            return_value="Python is a programming language.",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.handlers.ai_document._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("Python is great!", 10),
        ),
        patch("app.handlers.ai_document.handle_ai_response_error", new_callable=AsyncMock, return_value=False),
        patch("app.handlers.ai_document.send_long_message", new_callable=AsyncMock) as mock_send,
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "What is Python?", chat_state)

    mock_send.assert_awaited_once()


# ── No documents uploaded ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_no_documents():
    """Shows error when user has no uploaded documents."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.document_processor.get_user_documents", new_callable=AsyncMock, return_value=[]),
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
        patch("app.document_processor.get_document_content", new_callable=AsyncMock, return_value=None),
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
            "app.document_processor.get_document_content", new_callable=AsyncMock, return_value="Some document content"
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_document._get_ai_response_with_routing", new_callable=AsyncMock, return_value=(None, 0)),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    placeholder.edit_text.assert_awaited()
    text = placeholder.edit_text.call_args[0][0]
    assert "не удалось" in text.lower() or "ответ" in text.lower()


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

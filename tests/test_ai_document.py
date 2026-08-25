"""Tests for app.handlers.ai_document — document question handling."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.config as config
from app.errors import ErrorCode
from app.providers.stream_types import (
    FinishReason,
    GroundingReport,
    ProviderKind,
    RouteUsed,
    StreamCompleted,
    TokenUsage,
)
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


def _completion(actual_model: str = "gemini-3.1-flash-lite") -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=10),
        grounding=GroundingReport(),
        route=RouteUsed(
            provider=ProviderKind.GEMINI,
            requested_model="gemini-3.1-flash-lite",
            actual_model=actual_model,
        ),
    )


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
    monkeypatch.setattr("app.handlers.ai_document.settings", settings)
    return settings


@pytest.fixture(autouse=True)
def document_privacy_boundary_defaults():
    """Keep legacy behavior tests focused while the boundary is fail-closed."""

    @asynccontextmanager
    async def allowed_lease(*_args, **_kwargs):
        yield True

    async def current_generation(_user_id, *, expected_epoch):
        return 0 if expected_epoch is None else expected_epoch

    with (
        patch(
            "app.handlers.ai_document.ensure_chat_generation",
            side_effect=current_generation,
            create=True,
        ),
        patch("app.repos.memory_consent.private_data_lease", allowed_lease),
    ):
        yield


def make_placeholder():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat.id = 456
    msg.chat_id = 456
    msg.from_user.id = 123
    msg.get_bot.return_value = MagicMock()
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
        memory_epoch=0,
        private_data_blocked=False,
        _has_persisted_chat=True,
    )


# ── Happy path — AI answers question ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_document_question_holds_exact_epoch_lease_through_private_delivery():
    """Private document text cannot outlive its exact conversation generation."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()
    chat_state.memory_epoch = 37
    ensure_generation = AsyncMock(return_value=37)
    boundary = {
        "lease_active": False,
        "request_inside_lease": False,
        "delivery_inside_lease": False,
        "private_content": False,
        "allow_deferred": None,
    }
    lease_calls = []

    @asynccontextmanager
    async def tracked_lease(user_id, expected_epoch, *, purpose, require_ltm):
        lease_calls.append((user_id, expected_epoch, purpose, require_ltm))
        boundary["lease_active"] = True
        try:
            yield True
        finally:
            boundary["lease_active"] = False

    async def build_request(**kwargs):
        boundary["request_inside_lease"] = boundary["lease_active"]
        boundary["allow_deferred"] = kwargs["allow_deferred"]
        return SimpleNamespace(models=kwargs["models"])

    async def stream_response(target, _request, **_kwargs):
        boundary["delivery_inside_lease"] = boundary["lease_active"]
        boundary["private_content"] = target.private_content
        return CompleteDelivery(
            content_text="Private answer",
            displayed_text="Private answer",
            completion=_completion(),
            voice_requested=False,
            receipt=_receipt(),
        )

    delivery = MagicMock()
    delivery.stream = AsyncMock(side_effect=stream_response)

    with (
        patch(
            "app.handlers.ai_document.ensure_chat_generation",
            ensure_generation,
            create=True,
        ),
        patch("app.repos.memory_consent.private_data_lease", tracked_lease),
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "private.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value="Private document content",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.providers.request_factory.generation_request_from_history",
            side_effect=build_request,
        ),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Private question", chat_state)

    ensure_generation.assert_awaited_once_with(123, expected_epoch=37)
    assert lease_calls == [(123, 37, "conversation:document", False)]
    assert boundary == {
        "lease_active": False,
        "request_inside_lease": True,
        "delivery_inside_lease": True,
        "private_content": True,
        "allow_deferred": False,
    }


@pytest.mark.asyncio
async def test_document_question_denied_lease_never_reaches_provider_or_delivery():
    """An invalidated generation fails closed before private document egress."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()
    chat_state.memory_epoch = 11
    provider_request = AsyncMock()
    delivery = MagicMock()
    delivery.stream = AsyncMock()

    @asynccontextmanager
    async def denied_lease(*_args, **_kwargs):
        yield False

    with (
        patch(
            "app.handlers.ai_document.ensure_chat_generation",
            new_callable=AsyncMock,
            return_value=11,
            create=True,
        ),
        patch("app.repos.memory_consent.private_data_lease", denied_lease),
        patch(
            "app.document_processor.get_user_documents",
            new_callable=AsyncMock,
            return_value=[{"id": 1, "filename": "private.txt"}],
        ),
        patch(
            "app.document_processor.get_document_content",
            new_callable=AsyncMock,
            return_value="Private document content",
        ),
        patch("app.handlers.ai_document.update_stage", new_callable=AsyncMock),
        patch(
            "app.providers.request_factory.generation_request_from_history",
            provider_request,
        ),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Private question", chat_state)

    provider_request.assert_not_awaited()
    delivery.stream.assert_not_awaited()
    placeholder.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_question_success():
    """Successfully answers through the typed response-delivery boundary."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="Python is great!",
            displayed_text="Python is great!",
            completion=_completion(),
            voice_requested=False,
            receipt=_receipt(),
        )
    )

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
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "What is Python?", chat_state)

    request = delivery.stream.await_args.args[1]
    assert request.models == ("gemini-3.1-flash-lite",)
    reply_markup = delivery.stream.await_args.kwargs["presentation"].actions
    assert reply_markup is not None
    labels = [button.text for row in reply_markup.inline_keyboard for button in row]
    assert labels == [
        "📄 Загрузить другой документ",
        "📋 Выбрать документ",
        "❌ Отменить работу с документами",
        "🎭 Выбрать роль ИИ",
        "✨ Начать новую тему",
    ]
    mock_metrics.record_api_call.assert_awaited_once_with(
        "document_question", "gemini-3.1-flash-lite"
    )


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
    """Does not render a second error after typed delivery handled it."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=FailedDelivery(
            error_code=ErrorCode.EMPTY_RESPONSE,
            displayed_text="Пустой ответ",
            receipt=_receipt(),
        )
    )

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
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
    ):
        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    delivery.stream.assert_awaited_once()
    placeholder.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_question_records_actual_routed_model():
    """Metrics use the exact model reported by the typed route terminal."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="Fallback answer",
            displayed_text="Fallback answer",
            completion=_completion("opencode-go/qwen3.5-plus"),
            voice_requested=False,
            receipt=_receipt(),
        )
    )

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
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_document.metrics_collector") as mock_metrics,
    ):
        mock_metrics.record_api_call = AsyncMock()

        from app.handlers.ai_document import _handle_document_question

        await _handle_document_question(placeholder, 123, "Question", chat_state)

    mock_metrics.record_api_call.assert_awaited_once_with(
        "document_question", "opencode-go/qwen3.5-plus"
    )


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

"""Deferred worker uses the typed router and send-only Telegram delivery."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.errors import ErrorCode
from app.queue_contracts import JobOutcome
from app.response_delivery.outcomes import CompleteDelivery
from app.response_delivery.renderer import (
    DeliveryKind,
    DeliveryReceipt,
    TelegramMessageRef,
)


@pytest.mark.asyncio
async def test_deferred_worker_uses_response_delivery_without_requeueing():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    class BotContext:
        async def __aenter__(self):
            return bot

        async def __aexit__(self, exc_type, exc, tb):
            return False

    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="answer",
            displayed_text="answer",
            completion=None,
            voice_requested=False,
            receipt=DeliveryReceipt(
                kind=DeliveryKind.MESSAGE,
                message_ids=(1,),
                final_message=TelegramMessageRef(chat_id=10, message_id=1),
            ),
        )
    )

    with (
        patch("telegram.Bot", return_value=BotContext()),
        patch("app.providers.get_provider_router", return_value=MagicMock()),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
    ):
        from app.deferred_response import handle_deferred_ai_response

        result = await handle_deferred_ai_response(
            chat_id=10,
            history=[{"role": "user", "parts": ["hello"]}],
            model_name="gemini-3.5-flash",
            system_instruction="system",
        )

    assert result.outcome is JobOutcome.SUCCEEDED
    assert result.result_summary == {"text_length": 6, "partial": False}
    delivery.stream.assert_awaited_once()
    target, request = delivery.stream.await_args.args
    assert target.placeholder_message is None
    assert target.bot is bot
    assert request.allow_deferred is False
    assert request.workload.value == "deferred_retry"


@pytest.mark.parametrize(
    "kind, expected",
    [
        ("partial", JobOutcome.SUCCEEDED),
        ("temporary", JobOutcome.RETRYABLE_FAILURE),
        ("invalid", JobOutcome.PERMANENT_FAILURE),
        ("uncertain_transport", JobOutcome.UNKNOWN_DELIVERY),
    ],
)
async def test_deferred_worker_preserves_delivery_retry_safety(kind, expected):
    from app.deferred_response import handle_deferred_ai_response
    from app.response_delivery.outcomes import FailedDelivery, PartialDelivery

    bot = MagicMock(send_chat_action=AsyncMock())

    class BotContext:
        async def __aenter__(self):
            return bot

        async def __aexit__(self, *args):
            return False

    receipt = DeliveryReceipt(DeliveryKind.MESSAGE, (1,), TelegramMessageRef(10, 1))
    delivery = MagicMock()
    if kind == "uncertain_transport":
        delivery.stream = AsyncMock(side_effect=TimeoutError("private synthetic transport detail"))
    elif kind == "partial":
        delivery.stream = AsyncMock(return_value=PartialDelivery("part", "part", MagicMock(), False, receipt))
    else:
        code = ErrorCode.INVALID_REQUEST if kind == "invalid" else ErrorCode.OVERLOADED
        delivery.stream = AsyncMock(return_value=FailedDelivery(code, "failure notice", receipt))
    with (
        patch("telegram.Bot", return_value=BotContext()),
        patch("app.response_delivery.delivery.get_telegram_response_delivery", return_value=delivery),
    ):
        result = await handle_deferred_ai_response(
            chat_id=10, history=[{"role": "user", "parts": ["synthetic question"]}], model_name="gemini-3.5-flash"
        )
    assert result.outcome is expected
    assert delivery.stream.await_count == 1
    assert "private synthetic" not in str(result)
    if kind == "partial":
        assert result.result_summary == {"text_length": 4, "partial": True}


@pytest.mark.parametrize(
    "updates",
    [
        {"chat_id": None},
        {"chat_id": True},
        {"chat_id": "invalid"},
        {"history": []},
        {"history": "invalid"},
        {"history": ["invalid"]},
        {"history": [{"role": "unsupported", "parts": ["text"]}]},
        {"history": [{"role": {}, "parts": ["text"]}]},
        {"history": [{"role": "user", "parts": [{}]}]},
        {"model_name": " "},
        {"system_instruction": {}},
    ],
)
async def test_malformed_deferred_input_is_permanent_before_external_work(updates):
    from app.deferred_response import handle_deferred_ai_response

    payload = {"chat_id": 10, "history": [{"role": "user", "parts": ["hello"]}], "model_name": "gemini-3.5-flash"}
    payload.update(updates)
    with (
        patch("telegram.Bot", side_effect=AssertionError("validation must precede Bot creation")) as bot,
    ):
        result = await handle_deferred_ai_response(**payload)
    assert result.outcome is JobOutcome.PERMANENT_FAILURE
    assert result.reason_code == "invalid_deferred_parameters"
    assert bot.call_count == 0


async def test_deferred_request_preparation_outage_is_retryable_before_cooldown():
    from app.deferred_response import handle_deferred_ai_response

    with (
        patch("telegram.Bot", side_effect=AssertionError("preparation must precede Bot creation")) as bot,
        patch("app.providers.request_factory.generation_request_from_history", side_effect=TimeoutError),
    ):
        result = await handle_deferred_ai_response(
            chat_id=10, history=[{"role": "user", "parts": ["hello"]}], model_name="gemini-3.5-flash"
        )
    assert result.outcome is JobOutcome.RETRYABLE_FAILURE
    assert result.reason_code == "request_preparation_failed"
    assert bot.call_count == 0


async def test_bot_initialization_failure_has_no_uncertain_delivery():
    from app.deferred_response import handle_deferred_ai_response

    with (
        patch("telegram.Bot", side_effect=TimeoutError),
    ):
        result = await handle_deferred_ai_response(
            chat_id=10, history=[{"role": "user", "parts": ["hello"]}], model_name="gemini-3.5-flash"
        )
    assert result.outcome is JobOutcome.RETRYABLE_FAILURE
    assert result.reason_code == "delivery_not_started"

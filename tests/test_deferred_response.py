"""Deferred worker uses the typed router and send-only Telegram delivery."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        patch("app.deferred_response.asyncio.sleep", new_callable=AsyncMock),
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

    assert result == {"status": "completed", "text_length": 6}
    delivery.stream.assert_awaited_once()
    target, request = delivery.stream.await_args.args
    assert target.placeholder_message is None
    assert target.bot is bot
    assert request.allow_deferred is False
    assert request.workload.value == "deferred_retry"

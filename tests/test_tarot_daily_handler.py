"""User-facing contracts for the daily Tarot subscription controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.tarot_daily import tarot_daily_callback


def _update(action: str):
    query = SimpleNamespace(
        data=f"tarot_daily:{action}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
    ), query


@pytest.mark.asyncio
async def test_subscribe_confirmation_offers_a_working_unsubscribe_control() -> None:
    update, query = _update("subscribe")

    with patch(
        "app.repos.tarot_daily_subscriptions.upsert_tarot_subscription",
        new_callable=AsyncMock,
        return_value=True,
    ) as subscribe:
        await tarot_daily_callback(update, SimpleNamespace())

    subscribe.assert_awaited_once_with(user_id=42, is_subscribed=True)
    text = query.edit_message_text.await_args.args[0]
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "в разработке" not in text
    assert "/tarot_settings" not in text
    assert markup.inline_keyboard[0][0].callback_data == "tarot_daily:unsubscribe"


@pytest.mark.asyncio
async def test_unsubscribe_control_disables_delivery_and_offers_resubscribe() -> None:
    update, query = _update("unsubscribe")

    with patch(
        "app.repos.tarot_daily_subscriptions.unsubscribe_tarot",
        new_callable=AsyncMock,
        return_value=True,
    ) as unsubscribe:
        await tarot_daily_callback(update, SimpleNamespace())

    unsubscribe.assert_awaited_once_with(42)
    text = query.edit_message_text.await_args.args[0]
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "отключена" in text.lower()
    assert markup.inline_keyboard[0][0].callback_data == "tarot_daily:subscribe"

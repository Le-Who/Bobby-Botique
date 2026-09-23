from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest
from telegram.error import BadRequest, Forbidden

from app.handlers import daily_crocodile


def test_only_permanent_chat_errors_retire_daily_recipient() -> None:
    from app.games.daily_telegram_delivery import is_unreachable_chat_error

    assert is_unreachable_chat_error(BadRequest("Chat not found"))
    assert is_unreachable_chat_error(Forbidden("Forbidden: bot was blocked by the user"))
    assert not is_unreachable_chat_error(BadRequest("Wrong file identifier"))


@pytest.mark.asyncio
async def test_crocodile_photo_chat_not_found_does_not_retry_as_text() -> None:
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=BadRequest("Chat not found")), send_message=AsyncMock())
    with patch("app.handlers.daily_crocodile._get_placeholder_file_id", new=AsyncMock(return_value="cover")):
        with pytest.raises(BadRequest, match="Chat not found"):
            await daily_crocodile._send_daily_entry_message(
                bot,
                chat_id=7,
                user_id=7,
                puzzle_date=date(2026, 9, 23),
                caption="Daily",
                include_subscribe=False,
            )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreachable_subscriber_is_unsubscribed_and_discovery_is_snoozed() -> None:
    from app.games.daily_telegram_delivery import retire_unreachable_daily_recipient

    with (
        patch("app.games.daily_telegram_delivery.repo.unsubscribe", new=AsyncMock()) as unsubscribe,
        patch("app.games.daily_telegram_delivery.repo.snooze_discovery", new=AsyncMock()) as snooze,
    ):
        assert await retire_unreachable_daily_recipient(7, BadRequest("Chat not found"), discovery=False)
        assert await retire_unreachable_daily_recipient(8, BadRequest("Chat not found"), discovery=True)
        assert not await retire_unreachable_daily_recipient(9, BadRequest("Wrong file identifier"), discovery=False)
    unsubscribe.assert_awaited_once_with(7)
    assert snooze.await_args_list == [call(7), call(8)]

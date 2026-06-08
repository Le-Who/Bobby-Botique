from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.asyncio
async def test_generate_daily_tarot_uses_prepared_reading_without_provider_call():
    from app.handlers.inline import _generate_tarot_inline
    from app.tarot_daily import TarotDailyReading

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    prepared = TarotDailyReading(
        reading_date=date(2026, 6, 9),
        card_name="Маг",
        orientation="Прямая",
        language="ru",
        body_markdown="**Энергия дня:** действуйте собранно.",
        model_name="gemini-3.1-flash-lite",
    )

    with (
        patch(
            "app.tarot.get_tarot_context",
            return_value=(
                "Позиция «Карта дня»: Маг, Прямая.\nКлючевые слова: воля.\nЗначение: действие.",
                ["Маг (Прямая)"],
            ),
        ),
        patch("app.tarot_daily.get_prepared_daily_reading", new_callable=AsyncMock, return_value=prepared),
        patch("app.providers.router.get_provider_router") as router_factory,
    ):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="inline-1",
            user_query="таро",
            user_id=123,
            spread_type="tarot_daily",
        )

    router_factory.assert_not_called()
    bot.edit_message_text.assert_awaited_once()
    text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Карта дня" in text
    assert "Маг" in text
    assert "Энергия дня" in text


@pytest.mark.asyncio
async def test_prepare_daily_tarot_generates_missing_variants_with_rpm_delay():
    from app.tarot_daily import (
        TAROT_DAILY_MODEL,
        TAROT_DAILY_REQUEST_INTERVAL_SECONDS,
        prepare_daily_readings,
    )

    variants = [
        {
            "name": "Маг",
            "orientation": "Прямая",
            "context": "Позиция «Карта дня»: Маг, Прямая.",
            "label": "Маг (Прямая)",
        },
        {
            "name": "Маг",
            "orientation": "Перевернутая",
            "context": "Позиция «Карта дня»: Маг, Перевернутая.",
            "label": "Маг (Перевернутая)",
        },
    ]
    router = AsyncMock()
    router.get_response.return_value = ("Готовый текст карты дня.", 42)
    sleep = AsyncMock()

    with (
        patch("app.tarot_daily.iter_daily_card_variants", return_value=variants),
        patch("app.tarot_daily.get_provider_router", return_value=router),
        patch("app.tarot_daily.get_prepared_daily_reading", new_callable=AsyncMock, return_value=None),
        patch("app.tarot_daily.upsert_prepared_daily_reading", new_callable=AsyncMock) as upsert,
    ):
        result = await prepare_daily_readings(
            target_date=date(2026, 6, 9),
            sleep=sleep,
        )

    assert result.generated == 2
    assert result.skipped == 0
    assert router.get_response.await_count == 2
    assert upsert.await_count == 2
    assert sleep.await_count == 1
    assert sleep.await_args.args[0] >= TAROT_DAILY_REQUEST_INTERVAL_SECONDS
    first_call = router.get_response.await_args_list[0].kwargs
    assert first_call["preferred_model"] == TAROT_DAILY_MODEL
    assert first_call["max_key_retries"] >= 3


@pytest.mark.asyncio
async def test_prepare_daily_tarot_throttles_after_failed_generation():
    from app.tarot_daily import TAROT_DAILY_REQUEST_INTERVAL_SECONDS, prepare_daily_readings

    variants = [
        {
            "name": "Маг",
            "orientation": "Прямая",
            "context": "Позиция «Карта дня»: Маг, Прямая.",
            "label": "Маг (Прямая)",
        },
        {
            "name": "Жрица",
            "orientation": "Прямая",
            "context": "Позиция «Карта дня»: Жрица, Прямая.",
            "label": "Жрица (Прямая)",
        },
    ]
    router = AsyncMock()
    router.get_response.return_value = ("", 0)
    sleep = AsyncMock()

    with (
        patch("app.tarot_daily.iter_daily_card_variants", return_value=variants),
        patch("app.tarot_daily.get_provider_router", return_value=router),
        patch("app.tarot_daily.get_prepared_daily_reading", new_callable=AsyncMock, return_value=None),
        patch("app.tarot_daily.upsert_prepared_daily_reading", new_callable=AsyncMock) as upsert,
    ):
        result = await prepare_daily_readings(
            target_date=date(2026, 6, 9),
            sleep=sleep,
        )

    assert result.generated == 0
    assert result.failed == 2
    assert upsert.await_count == 0
    assert sleep.await_count == 1
    assert sleep.await_args.args[0] >= TAROT_DAILY_REQUEST_INTERVAL_SECONDS


def test_tarot_daily_preparation_window_is_pacific_evening():
    from app.tarot_daily import is_preparation_window

    pacific = ZoneInfo("America/Los_Angeles")

    assert is_preparation_window(datetime(2026, 6, 9, 22, 0, tzinfo=pacific))
    assert is_preparation_window(datetime(2026, 6, 9, 23, 45, tzinfo=pacific))
    assert not is_preparation_window(datetime(2026, 6, 9, 18, 0, tzinfo=pacific))


@pytest.mark.asyncio
async def test_tarot_daily_job_runs_only_in_preparation_window():
    from app.handlers.tarot_daily import check_tarot_daily_jobs

    context = SimpleNamespace()

    with (
        patch("app.handlers.tarot_daily.is_preparation_window", return_value=False),
        patch("app.handlers.tarot_daily.prepare_daily_readings", new_callable=AsyncMock) as prepare,
    ):
        await check_tarot_daily_jobs(context)

    prepare.assert_not_called()

    with (
        patch("app.handlers.tarot_daily.is_preparation_window", return_value=True),
        patch("app.handlers.tarot_daily.today_reading_date", return_value=date(2026, 6, 9)),
        patch("app.handlers.tarot_daily.prepare_daily_readings", new_callable=AsyncMock) as prepare,
    ):
        await check_tarot_daily_jobs(context)

    assert prepare.await_count == 2
    assert prepare.await_args_list[0].kwargs["target_date"] == date(2026, 6, 9)
    assert prepare.await_args_list[1].kwargs["target_date"] == date(2026, 6, 10)

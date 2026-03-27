"""Tests for app.handlers.cmd_reminders — time parser and command handler."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.cmd_reminders import _parse_reminder_args


class TestParseReminderArgs:
    """Test the bilingual time parser."""

    # -- English units --

    def test_minutes_m(self):
        delta, prompt = _parse_reminder_args("30m Проверить результаты")
        assert delta == timedelta(minutes=30)
        assert prompt == "Проверить результаты"

    def test_minutes_min(self):
        delta, prompt = _parse_reminder_args("15min Do something")
        assert delta == timedelta(minutes=15)
        assert prompt == "Do something"

    def test_minutes_mins(self):
        delta, prompt = _parse_reminder_args("5mins check logs")
        assert delta == timedelta(minutes=5)
        assert prompt == "check logs"

    def test_hours_h(self):
        delta, prompt = _parse_reminder_args("2h Написать отчёт")
        assert delta == timedelta(hours=2)
        assert prompt == "Написать отчёт"

    def test_hours_hr(self):
        delta, prompt = _parse_reminder_args("1hr meeting")
        assert delta == timedelta(hours=1)
        assert prompt == "meeting"

    def test_hours_hours(self):
        delta, prompt = _parse_reminder_args("3hours review PR")
        assert delta == timedelta(hours=3)
        assert prompt == "review PR"

    def test_days_d(self):
        delta, prompt = _parse_reminder_args("1d Созвон с командой")
        assert delta == timedelta(days=1)
        assert prompt == "Созвон с командой"

    def test_days_day(self):
        delta, prompt = _parse_reminder_args("2day followup")
        assert delta == timedelta(days=2)
        assert prompt == "followup"

    # -- Russian units --

    def test_russian_мин(self):
        delta, prompt = _parse_reminder_args("45мин проверить почту")
        assert delta == timedelta(minutes=45)
        assert prompt == "проверить почту"

    def test_russian_минут(self):
        delta, prompt = _parse_reminder_args("10минут тест")
        assert delta == timedelta(minutes=10)
        assert prompt == "тест"

    def test_russian_час(self):
        delta, prompt = _parse_reminder_args("1час обед")
        assert delta == timedelta(hours=1)
        assert prompt == "обед"

    def test_russian_часа(self):
        delta, prompt = _parse_reminder_args("2часа звонок")
        assert delta == timedelta(hours=2)
        assert prompt == "звонок"

    def test_russian_часов(self):
        delta, prompt = _parse_reminder_args("5часов подготовка")
        assert delta == timedelta(hours=5)
        assert prompt == "подготовка"

    def test_russian_день(self):
        delta, prompt = _parse_reminder_args("1день встреча")
        assert delta == timedelta(days=1)
        assert prompt == "встреча"

    def test_russian_дня(self):
        delta, prompt = _parse_reminder_args("3дня дедлайн")
        assert delta == timedelta(days=3)
        assert prompt == "дедлайн"

    def test_russian_дней(self):
        delta, prompt = _parse_reminder_args("7дней отчёт")
        assert delta == timedelta(days=7)
        assert prompt == "отчёт"

    # -- Edge cases --

    def test_whitespace_between_number_and_unit(self):
        delta, prompt = _parse_reminder_args("30 m reminder text")
        assert delta == timedelta(minutes=30)
        assert prompt == "reminder text"

    def test_invalid_format_returns_none(self):
        delta, prompt = _parse_reminder_args("invalid input")
        assert delta is None
        assert prompt is None

    def test_empty_string_returns_none(self):
        delta, prompt = _parse_reminder_args("")
        assert delta is None
        assert prompt is None

    def test_zero_amount_returns_none(self):
        delta, prompt = _parse_reminder_args("0m some text")
        assert delta is None
        assert prompt is None

    def test_huge_amount_returns_none(self):
        """Amounts > 365*24 should be rejected."""
        delta, prompt = _parse_reminder_args("999999h text")
        assert delta is None
        assert prompt is None

    def test_prompt_preserves_content(self):
        """The prompt portion should be preserved exactly."""
        delta, prompt = _parse_reminder_args("1h   multi word prompt with  spaces ")
        assert delta == timedelta(hours=1)
        assert prompt == "multi word prompt with  spaces"


class TestRemindCommand:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        """Calling /remind with no args should show usage text."""
        from app.handlers.cmd_reminders import remind_command

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = []

        # Bypass @authorized_only and @safe_handler decorators
        inner = remind_command.__wrapped__.__wrapped__

        with patch("app.handlers.cmd_reminders.get_user_reminders", new_callable=AsyncMock, return_value=[]):
            await inner(update, context)

        update.message.reply_text.assert_awaited_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "remind" in call_text.lower() or "Формат" in call_text


class TestCheckAndDeliverReminders:
    @pytest.mark.asyncio
    async def test_no_pending_does_nothing(self):
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        with patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=[]):
            await check_and_deliver_reminders(context)

        context.bot.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delivers_pending_reminder(self):
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        pending = [{"id": 1, "user_id": 42, "prompt": "Test reminder"}]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
        ):
            await check_and_deliver_reminders(context)

        context.bot.send_message.assert_awaited_once()
        mock_mark.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_raise(self):
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock(side_effect=Exception("Network error"))

        pending = [{"id": 1, "user_id": 42, "prompt": "Test"}]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
        ):
            await check_and_deliver_reminders(context)  # Should not raise

        mock_mark.assert_not_awaited()

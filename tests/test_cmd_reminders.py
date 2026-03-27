"""Tests for app.handlers.cmd_reminders — time parser, intent classifier, and delivery."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.cmd_reminders import _classify_reminder_intent, _parse_reminder_args


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


# ── Intent classifier ────────────────────────────────────────────────────────


class TestClassifyReminderIntent:
    """Test the heuristic intent classifier."""

    # -- Plain text (no AI) --

    @pytest.mark.parametrize(
        "prompt",
        [
            "Покорми кота",
            "Выключи духовку",
            "Купи молоко",
            "Позвони маме",
            "Созвон с командой",
            "Забери посылку",
            "Прими таблетку",
            "Call mom",
            "Buy groceries",
            "Turn off the oven",
        ],
    )
    def test_plain_text_not_ai(self, prompt):
        result = _classify_reminder_intent(prompt)
        assert result["is_ai"] is False
        assert result["mode"] == "notify"

    # -- QnA search --

    @pytest.mark.parametrize(
        "prompt",
        [
            "Проверь курс доллара",
            "Найди новости о Tesla",
            "Узнай погоду в Москве",
            "Поищи актуальные новости",
            "Check latest Bitcoin price",
            "Find news about OpenAI",
            "Search for weather forecast",
        ],
    )
    def test_qna_search(self, prompt):
        result = _classify_reminder_intent(prompt)
        assert result["is_ai"] is True
        assert result["mode"] == "qna"

    # -- Research (deep analysis) --

    @pytest.mark.parametrize(
        "prompt",
        [
            "Проанализируй тренды AI за последний месяц",
            "Исследуй рынок электромобилей в Европе",
            "Сравни характеристики iPhone и Samsung",
            "Research the latest developments in quantum computing and compare approaches",
            "Сделай глубокий анализ акций Apple и сравни с конкурентами, финансовые отчёты и дивиденды",
        ],
    )
    def test_research_mode(self, prompt):
        result = _classify_reminder_intent(prompt)
        assert result["is_ai"] is True
        assert result["mode"] == "research"

    def test_long_ai_prompt_triggers_research(self):
        """Prompts >80 chars with AI signals should be classified as research."""
        long_prompt = "Найди подробную информацию о последних изменениях в законодательстве и как они повлияют на бизнес"
        result = _classify_reminder_intent(long_prompt)
        assert result["is_ai"] is True
        assert result["mode"] == "research"

    def test_short_ambiguous_defaults_to_notify(self):
        """Short prompts without clear AI keywords should default to notify."""
        result = _classify_reminder_intent("чай")
        assert result["is_ai"] is False
        assert result["mode"] == "notify"

    def test_classifier_returns_valid_structure(self):
        """All results must have is_ai and mode keys."""
        for prompt in ["foo", "найди X", "проанализируй Y"]:
            result = _classify_reminder_intent(prompt)
            assert "is_ai" in result
            assert "mode" in result
            assert result["mode"] in ("notify", "qna", "research")


# ── Remind command ───────────────────────────────────────────────────────────


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


# ── Delivery ─────────────────────────────────────────────────────────────────


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
    async def test_delivers_plain_text_reminder(self):
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        pending = [{"id": 1, "user_id": 42, "prompt": "Test reminder", "context_history": None}]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
        ):
            await check_and_deliver_reminders(context)

        context.bot.send_message.assert_awaited_once()
        mock_mark.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_dispatches_ai_reminder(self):
        """AI reminders should fire asyncio.create_task and mark delivered."""
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        pending = [{
            "id": 2,
            "user_id": 42,
            "prompt": "Find latest news",
            "context_history": {"is_ai": True, "mode": "qna"},
        }]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
            patch("asyncio.create_task") as mock_create_task,
        ):
            await check_and_deliver_reminders(context)

        # AI reminder should NOT call bot.send_message directly (that's handled by _execute_ai_reminder)
        context.bot.send_message.assert_not_awaited()
        mock_create_task.assert_called_once()
        mock_mark.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_dispatches_ai_reminder_from_json_string(self):
        """AI metadata stored as a JSON string (legacy) should be parsed correctly."""
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock()

        pending = [{
            "id": 3,
            "user_id": 42,
            "prompt": "Research AI trends",
            "context_history": '{"is_ai": true, "mode": "research"}',
        }]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
            patch("asyncio.create_task") as mock_create_task,
        ):
            await check_and_deliver_reminders(context)

        mock_create_task.assert_called_once()
        mock_mark.assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_raise(self):
        from app.handlers.cmd_reminders import check_and_deliver_reminders

        context = MagicMock()
        context.bot.send_message = AsyncMock(side_effect=Exception("Network error"))

        pending = [{"id": 1, "user_id": 42, "prompt": "Test", "context_history": None}]

        with (
            patch("app.handlers.cmd_reminders.get_pending_reminders", new_callable=AsyncMock, return_value=pending),
            patch("app.handlers.cmd_reminders.mark_delivered", new_callable=AsyncMock) as mock_mark,
        ):
            await check_and_deliver_reminders(context)  # Should not raise

        mock_mark.assert_not_awaited()


# ── Cancel callback ──────────────────────────────────────────────────────────


class TestReminderCancelCallback:
    @pytest.mark.asyncio
    async def test_cancel_deletes_and_updates_buttons(self):
        """Pressing ❌ should delete the reminder and remove the button."""
        from app.handlers.cmd_reminders import reminder_cancel_callback

        query = MagicMock()
        query.data = "reminder_cancel:42"
        query.from_user.id = 100
        query.answer = AsyncMock()
        query.message.reply_markup.inline_keyboard = [
            [MagicMock(callback_data="reminder_cancel:42")],
            [MagicMock(callback_data="reminder_cancel:99")],
        ]
        query.message.edit_reply_markup = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("app.handlers.cmd_reminders.delete_reminder", new_callable=AsyncMock, return_value=True):
            await reminder_cancel_callback(update, context)

        # Button for id=42 should be removed, id=99 should remain
        query.message.edit_reply_markup.assert_awaited_once()
        new_markup = query.message.edit_reply_markup.call_args[1]["reply_markup"]
        remaining = [btn.callback_data for row in new_markup.inline_keyboard for btn in row]
        assert "reminder_cancel:42" not in remaining
        assert "reminder_cancel:99" in remaining

    @pytest.mark.asyncio
    async def test_cancel_shows_error_on_failure(self):
        """Failed deletion should show error alert."""
        from app.handlers.cmd_reminders import reminder_cancel_callback

        query = MagicMock()
        query.data = "reminder_cancel:42"
        query.from_user.id = 100
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("app.handlers.cmd_reminders.delete_reminder", new_callable=AsyncMock, return_value=False):
            await reminder_cancel_callback(update, context)

        # Should show error alert (the second query.answer call with show_alert=True)
        calls = query.answer.call_args_list
        assert any(c.kwargs.get("show_alert") is True for c in calls)

    @pytest.mark.asyncio
    async def test_cancel_invalid_data_does_nothing(self):
        """Invalid callback data should be handled gracefully."""
        from app.handlers.cmd_reminders import reminder_cancel_callback

        query = MagicMock()
        query.data = "reminder_cancel:notanumber"
        query.from_user.id = 100
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch("app.handlers.cmd_reminders.delete_reminder", new_callable=AsyncMock) as mock_delete:
            await reminder_cancel_callback(update, context)

        mock_delete.assert_not_awaited()

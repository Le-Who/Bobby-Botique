import asyncio
import time
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Chat, InlineKeyboardMarkup, Message, ReplyKeyboardRemove, Update, User
from telegram.ext import Application, ConversationHandler

from app import state
from app.handlers import cmd_tarot, commands, inline


def _update(text="/start compat_m7_f10", chat_type="private", *, message_id=1, edited=False):
    user = SimpleNamespace(id=98765001, language_code="ru", first_name="Test", username="test")
    chat = SimpleNamespace(id=user.id if chat_type == "private" else -123, type=chat_type)
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), chat=chat, from_user=user, message_id=message_id)
    return SimpleNamespace(
        message=None if edited else message,
        edited_message=message if edited else None,
        effective_message=message,
        effective_user=user,
        effective_chat=chat,
        callback_query=None,
        update_id=123,
    )


def _context(payload="compat_m7_f10"):
    return SimpleNamespace(
        args=[payload],
        user_data={},
        bot=SimpleNamespace(send_message=AsyncMock(), username="test_bot"),
        application=SimpleNamespace(),
    )


@pytest.fixture(autouse=True)
def _isolated_bot_state(monkeypatch):
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(state, "_schedule_persist", lambda _: None)
    loaded = AsyncMock()
    monkeypatch.setattr(state, "ensure_state_loaded", loaded)
    monkeypatch.setattr(cmd_tarot, "ensure_state_loaded", loaded)
    monkeypatch.setattr(
        commands, "get_user_chat", AsyncMock(side_effect=AssertionError("Unexpected general start menu"))
    )
    state.clear_tarot_session(98765001)
    yield
    state.clear_tarot_session(98765001)


@pytest.mark.asyncio
async def test_inline_reading_is_immediate_and_has_two_contextual_deep_links():
    query = SimpleNamespace(
        query="совм мужчина скорпион женщина водолей",
        from_user=SimpleNamespace(id=42, language_code="en"),
        answer=AsyncMock(),
    )
    context = SimpleNamespace(bot=SimpleNamespace(first_name="Test", username="test_bot"))

    await inline.handle_inline_query(SimpleNamespace(inline_query=query), context)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].id == "compatibility"
    assert "Скорпион" in results[0].input_message_content.message_text
    assert "Водолей" in results[0].input_message_content.message_text
    assert "Сильная сторона" in results[0].input_message_content.message_text
    buttons = results[0].reply_markup.inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 2
    assert buttons[0][0].url == "https://t.me/test_bot?start=compat_m7_f10"
    assert buttons[0][1].url == "https://t.me/test_bot?start=compat_t_m7_f10"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["совм", "совм скорпион", "совм скорпион водолей рыбы"])
async def test_incomplete_inline_query_shows_help_instead_of_general_ai(text):
    query = SimpleNamespace(query=text, from_user=SimpleNamespace(id=42, language_code="ru"), answer=AsyncMock())
    context = SimpleNamespace(bot=SimpleNamespace(first_name="Test", username="test_bot"))

    await inline.handle_inline_query(SimpleNamespace(inline_query=query), context)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].id == "compatibility_hint"
    assert "два" in results[0].title.lower()


@pytest.mark.asyncio
async def test_chosen_compatibility_result_does_not_start_duplicate_generation(monkeypatch):
    called = []
    monkeypatch.setattr(inline, "_generate_and_edit_inline", lambda **kwargs: called.append(kwargs))
    chosen = SimpleNamespace(
        result_id="compatibility",
        query="совм скорпион водолей",
        inline_message_id="id",
        from_user=SimpleNamespace(id=42),
    )

    await inline.handle_chosen_inline_result(SimpleNamespace(chosen_inline_result=chosen), _context())

    assert called == []


@pytest.mark.asyncio
async def test_start_link_enters_private_date_flow_and_clears_tarot_mode():
    state.set_tarot_mode(98765001, True)
    update, context = _update(), _context()

    await commands.start_command(update, context)

    flow = context.user_data["compatibility_flow"]
    assert flow["pair"].first.sign_index == 7
    assert flow["pair"].second.sign_index == 10
    assert flow.get("first_date") is None
    assert not state.is_in_tarot_mode(update.effective_user.id)
    text = update.message.reply_text.await_args.args[0]
    assert "Шаг 1 из 2" in text
    assert "Мужчина · Скорпион" in text
    assert isinstance(update.message.reply_text.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)


@pytest.mark.asyncio
async def test_date_flow_validates_each_partner_and_erases_raw_dates_after_result():
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    update.message.reply_text.reset_mock()
    update.message.text = "31.02.2003"
    await handler.handle_compatibility_date(update, context)
    assert context.user_data["compatibility_flow"].get("first_date") is None
    assert "Дата не распознана" in update.message.reply_text.await_args.args[0]

    update.message.text = "30.06.2003"
    await handler.handle_compatibility_date(update, context)
    assert "Шаг 2 из 2" in update.message.reply_text.await_args.args[0]
    update.message.text = "09.11.1997"
    await handler.handle_compatibility_date(update, context)

    assert "compatibility_flow" not in context.user_data
    assert "Рак" in update.message.reply_text.await_args.args[0]
    assert isinstance(update.message.reply_text.await_args.kwargs["reply_markup"], InlineKeyboardMarkup)
    stored = next(iter(context.user_data["compatibility_tarot_context"].values()))
    context_text = stored["question"]
    assert "2003-06-30" not in context_text and "1997-11-09" not in context_text


@pytest.mark.asyncio
async def test_tarot_link_enters_existing_mode_with_the_same_pair():
    update, context = _update("/start compat_t_m7_f10"), _context("compat_t_m7_f10")

    await commands.start_command(update, context)

    assert state.is_in_tarot_mode(update.effective_user.id)
    session = state.get_tarot_session(update.effective_user.id)
    assert session["waiting_for_question"] is True
    assert session["drawn_cards"] == []
    assert "Скорпион" in session["question_context"] and "Водолей" in session["question_context"]
    buttons = context.bot.send_message.await_args.kwargs["reply_markup"].keyboard
    assert any("совместимость" in button.text for row in buttons for button in row)


@pytest.mark.asyncio
async def test_start_links_do_not_collect_birth_dates_or_change_tarot_in_groups():
    update, context = _update(chat_type="supergroup"), _context()

    await commands.start_command(update, context)

    assert "compatibility_flow" not in context.user_data
    assert not state.is_in_tarot_mode(update.effective_user.id)
    assert "личном" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_expired_flow_drops_the_first_date_without_generating_a_reading():
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    flow = context.user_data["compatibility_flow"]
    flow["started_at"] = time.monotonic() - 3600
    flow["first_date"] = "2003-06-30"
    update.message.text = "09.11.1997"
    await handler.handle_compatibility_date(update, context)

    assert "compatibility_flow" not in context.user_data
    assert "времени" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_two_private_results_keep_their_own_tarot_context_when_older_button_is_clicked():
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    for birthday in ("30.06.2003", "09.11.1997"):
        update.message.text = birthday
        await handler.handle_compatibility_date(update, context)
    first_callback = update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    update.message.text = "/start compat_m0_f6"
    context.args = ["compat_m0_f6"]
    await commands.start_command(update, context)
    for birthday in ("10.04.2001", "10.10.2001"):
        update.message.text = birthday
        await handler.handle_compatibility_date(update, context)
    second_callback = update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert first_callback != second_callback
    update.callback_query = SimpleNamespace(data=first_callback, answer=AsyncMock(), message=update.message, id="cb")
    await handler.compatibility_tarot_callback(update, context)

    session = state.get_tarot_session(update.effective_user.id)
    assert "Солнце: Рак" in session["question_context"]
    assert "Солнце: Весы" not in session["question_context"]


@pytest.mark.asyncio
async def test_revoked_access_erases_birth_input_without_alerting_an_admin_with_the_date(monkeypatch):
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    update.message.text = "30.06.2003"
    await handler.handle_compatibility_date(update, context)
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=False))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    alerts = AsyncMock()
    monkeypatch.setattr("app.admin_alerts.alert_admin_unauthorized_user", alerts)
    update.message.text = "09.11.1997"
    await handler.handle_compatibility_date(update, context)

    assert "compatibility_flow" not in context.user_data
    assert alerts.call_count == 0


@pytest.mark.asyncio
async def test_other_command_abandons_dates_and_cancel_removes_pending_input():
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    update.message.text = "30.06.2003"
    await handler.handle_compatibility_date(update, context)
    update.message.text = "/model"
    await handler.clear_compatibility_on_command(update, context)
    assert "compatibility_flow" not in context.user_data
    await commands.start_command(update, context)
    await handler.cancel_compatibility(update, context)
    assert "compatibility_flow" not in context.user_data


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["compat_t_m7_f10", "compat_t_e_m7_f10"])
async def test_first_tarot_draw_sends_pair_context_instead_of_the_shortcut_label(monkeypatch, payload):
    from app.handlers import tarot_chat

    update, context = _update(f"/start {payload}"), _context(payload)
    await commands.start_command(update, context)
    question_context = state.get_tarot_session(update.effective_user.id)["question_context"]
    draw_button = context.bot.send_message.await_args.kwargs["reply_markup"].keyboard[0][0].text
    update.message.text = draw_button
    update.message.reply_text.return_value = SimpleNamespace(delete=AsyncMock())
    captured = {}

    async def fake_execute(*args, history, **kwargs):
        captured["user_question"] = history[0]["parts"][0]
        return "Символический разбор", 10

    monkeypatch.setattr(tarot_chat, "get_provider_router", lambda: SimpleNamespace(get_response=fake_execute))
    await tarot_chat.handle_tarot_message(update, context)

    assert captured["user_question"] == question_context
    assert len(state.get_tarot_session(update.effective_user.id)["drawn_cards"]) == 3
    assert not state.get_tarot_session(update.effective_user.id)["waiting_for_question"]


@pytest.mark.asyncio
async def test_editing_first_date_replaces_first_partner_instead_of_finishing_the_pair():
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    edited = _update("01.07.2003", message_id=2, edited=True)

    await handler.handle_compatibility_date(edited, context)

    flow = context.user_data["compatibility_flow"]
    assert flow["first_date"] == date(2003, 7, 1)
    assert "Шаг 2 из 2" in edited.effective_message.reply_text.await_args.args[0]
    assert "compatibility_tarot_context" not in context.user_data


@pytest.mark.asyncio
async def test_completed_birth_messages_edits_stay_out_of_normal_ai_routing():
    from app.handlers import compatibility as handler

    context = _context()
    await commands.start_command(_update(), context)
    for message_id, birthday in ((2, "30.06.2003"), (3, "09.11.1997")):
        await handler.handle_compatibility_date(_update(birthday, message_id=message_id), context)
    application = SimpleNamespace(user_data={98765001: context.user_data})
    edited = _update("01.07.2003", message_id=2, edited=True)

    assert handler.CompatibilityEditedFilter(application).filter(edited.effective_message)
    await handler.handle_compatibility_date(edited, context)
    assert "заново" in edited.effective_message.reply_text.await_args.args[0]
    assert "compatibility_flow" not in context.user_data
    assert not handler.CompatibilityEditedFilter(application).filter(_update("обычный текст", message_id=4).message)


@pytest.mark.asyncio
async def test_edit_of_older_pair_does_not_replace_a_new_pairs_first_date():
    from app.handlers import compatibility as handler

    context = _context()
    await commands.start_command(_update(), context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    await handler.cancel_compatibility(_update("/cancel", message_id=3), context)
    await commands.start_command(_update(message_id=4), context)
    await handler.handle_compatibility_date(_update("09.11.1997", message_id=5), context)

    await handler.handle_compatibility_date(_update("01.07.2003", message_id=2, edited=True), context)

    assert context.user_data["compatibility_flow"]["first_date"] == date(1997, 11, 9)


@pytest.mark.asyncio
async def test_cancel_erases_dates_even_after_access_is_revoked(monkeypatch):
    from app.handlers import compatibility as handler

    update, context = _update(), _context()
    await commands.start_command(update, context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=False))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))

    await handler.cancel_compatibility(_update("/cancel", message_id=3), context)

    assert "compatibility_flow" not in context.user_data


@pytest.mark.asyncio
async def test_expiry_removes_unattended_dates_and_consumes_a_late_second_date(monkeypatch):
    from app.handlers import compatibility as handler

    monkeypatch.setattr(handler, "_FLOW_TTL", 0.02)
    context = _context()
    await commands.start_command(_update(), context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    await asyncio.sleep(0.04)

    flow = context.user_data["compatibility_flow"]
    assert "first_date" not in flow
    assert handler.PendingCompatibilityFilter(SimpleNamespace(user_data={98765001: context.user_data})).filter(
        _update("09.11.1997").message
    )
    late = _update("09.11.1997", message_id=3)
    await handler.handle_compatibility_date(late, context)
    assert "времени" in late.effective_message.reply_text.await_args.args[0]
    assert "compatibility_flow" not in context.user_data


@pytest.mark.asyncio
async def test_private_tarot_button_loads_state_and_abandons_another_date_flow():
    from app.handlers import compatibility as handler

    context = _context()
    await commands.start_command(_update(), context)
    for message_id, birthday in ((2, "30.06.2003"), (3, "09.11.1997")):
        result = _update(birthday, message_id=message_id)
        await handler.handle_compatibility_date(result, context)
    callback = result.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await commands.start_command(_update(message_id=4), context)
    await handler.handle_compatibility_date(_update("10.10.2001", message_id=5), context)
    state.ensure_state_loaded.reset_mock()
    clicked = _update(message_id=3)
    clicked.callback_query = SimpleNamespace(data=callback, answer=AsyncMock(), message=clicked.message, id="cb")

    await handler.compatibility_tarot_callback(clicked, context)

    state.ensure_state_loaded.assert_awaited_once_with(98765001)
    assert "compatibility_flow" not in context.user_data
    assert state.is_in_tarot_mode(98765001)
    assert "Солнце: Рак" in state.get_tarot_session(98765001)["question_context"]


def _telegram_update(text, *, message_id=2, edited=False, callback=None):
    user = User(id=98765001, first_name="Test", is_bot=False)
    chat = Chat(id=user.id, type="private")
    message = Message(message_id=message_id, date=datetime.now(UTC), chat=chat, from_user=user, text=text)
    if callback:
        return Update(
            update_id=123,
            callback_query=CallbackQuery(id="cb", from_user=user, chat_instance="chat", message=message, data=callback),
        )
    return Update(update_id=123, **{"edited_message" if edited else "message": message})


@pytest.mark.asyncio
async def test_registered_edit_handler_precedes_general_ai_before_and_after_completion():
    from app.handlers import compatibility as handler
    from app.handlers import messages

    context = _context()
    await commands.start_command(_update(), context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    application = Application.builder().token("123:offline").build()
    application.user_data[98765001].update(context.user_data)
    handler.register_compatibility_handlers(application)
    messages.register(application)
    edited = _telegram_update("01.07.2003", edited=True)

    selected = next(item for item in application.handlers[0] if item.check_update(edited))
    assert selected.callback is handler.handle_compatibility_date
    application.user_data[98765001].pop("compatibility_flow")
    selected = next(item for item in application.handlers[0] if item.check_update(edited))
    assert selected.callback is handler.handle_compatibility_date
    ordinary_edit = _telegram_update("обычный вопрос", message_id=8, edited=True)
    selected = next(item for item in application.handlers[0] if item.check_update(ordinary_edit))
    assert selected.callback is messages.handle_edited_request


@pytest.mark.asyncio
async def test_compatibility_tarot_callback_ends_active_natal_conversation():
    from app.handlers import compatibility as handler
    from app.handlers import natal_chart

    context = _context()
    await commands.start_command(_update(), context)
    for message_id, birthday in ((2, "30.06.2003"), (3, "09.11.1997")):
        result = _update(birthday, message_id=message_id)
        await handler.handle_compatibility_date(result, context)
    callback = result.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    context.user_data["natal_date"] = "2003-06-30"
    application = Application.builder().token("123:offline").build()
    commands.register(application)
    conversation = next(
        item for item in application.handlers[0] if isinstance(item, ConversationHandler) and item.name == "natal_chart"
    )
    conversation._conversations[(98765001, 98765001)] = natal_chart.NATAL_DATE
    actual = _telegram_update("result", message_id=3, callback=callback)

    selected = next(item for item in application.handlers[0] if item.check_update(actual))
    assert selected is conversation
    matched = conversation.check_update(actual)
    _, _, fallback, _ = matched
    fake = _update(message_id=3)
    fake.callback_query = SimpleNamespace(data=callback, answer=AsyncMock(), message=fake.message, id="cb")
    assert await fallback.callback(fake, context) == ConversationHandler.END
    assert "natal_date" not in context.user_data
    assert state.is_in_tarot_mode(98765001)


@pytest.mark.asyncio
async def test_natal_entry_callback_clears_another_pending_birth_flow(monkeypatch):
    from app.handlers import compatibility as handler
    from app.handlers import natal_chart

    context = _context()
    await commands.start_command(_update(), context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    monkeypatch.setattr(natal_chart, "_natal_reports_enabled_for_handler", lambda: True)
    callback = _update("Open natal", message_id=3)
    callback.callback_query = SimpleNamespace(data="start_natal", answer=AsyncMock(), message=callback.message, id="cb")
    await natal_chart.natal_command(callback, context)

    assert "compatibility_flow" not in context.user_data


@pytest.mark.asyncio
async def test_regular_tarot_entry_clears_pending_dates():
    from app.handlers import cmd_tarot
    from app.handlers import compatibility as handler

    context = _context()
    await commands.start_command(_update(), context)
    await handler.handle_compatibility_date(_update("30.06.2003", message_id=2), context)
    callback = _update("Open tarot", message_id=3)
    callback.callback_query = SimpleNamespace(data="start_tarot", answer=AsyncMock(), message=callback.message, id="cb")

    await cmd_tarot.tarot_command(callback, context)

    assert "compatibility_flow" not in context.user_data
    assert state.is_in_tarot_mode(98765001)


@pytest.mark.asyncio
async def test_revoked_compatibility_callback_erases_pending_dates(monkeypatch):
    from app.handlers import compatibility as handler

    context = _context()
    await commands.start_command(_update(), context)
    for message_id, birthday in ((2, "30.06.2003"), (3, "09.11.1997")):
        result = _update(birthday, message_id=message_id)
        await handler.handle_compatibility_date(result, context)
    callback_data = (
        result.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    )
    await commands.start_command(_update(message_id=4), context)
    await handler.handle_compatibility_date(_update("10.10.2001", message_id=5), context)
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=False))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    callback = _update("result", message_id=3)
    callback.callback_query = SimpleNamespace(data=callback_data, answer=AsyncMock(), message=callback.message, id="cb")

    await handler.compatibility_tarot_callback(callback, context)

    assert "compatibility_flow" not in context.user_data
    assert not state.is_in_tarot_mode(98765001)


def test_regular_tarot_button_routes_through_active_natal_conversation():
    from app.handlers import natal_chart

    application = Application.builder().token("123:offline").build()
    commands.register(application)
    conversation = next(
        item for item in application.handlers[0] if isinstance(item, ConversationHandler) and item.name == "natal_chart"
    )
    conversation._conversations[(98765001, 98765001)] = natal_chart.NATAL_DATE
    actual = _telegram_update("menu", callback="start_tarot")

    selected = next(item for item in application.handlers[0] if item.check_update(actual))
    assert selected is conversation


@pytest.mark.parametrize("edited", [False, True])
@pytest.mark.parametrize("lost_context", ["restart", "evicted_marker"])
def test_orphan_date_never_reaches_ai_after_restart_or_marker_eviction(edited, lost_context):
    from app.handlers import compatibility as handler
    from app.handlers import messages

    application = Application.builder().token("123:offline").build()
    user_data = application.user_data[98765001]
    if lost_context == "evicted_marker":
        for message_id in range(2, 2 + handler._BIRTH_MESSAGE_LIMIT + 1):
            handler._remember_birth_message(user_data, message_id)
        assert 2 not in user_data["compatibility_birth_messages"]
    commands.register(application)
    messages.register(application)
    actual = _telegram_update("09.11.1997", message_id=2, edited=edited)

    selected = next(item for item in application.handlers[0] if item.check_update(actual))

    assert selected.callback is handler.handle_orphan_birth_date


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["09.11.1997", "1997-11-09", "31.02.1997", "1997-02-31"])
async def test_orphan_date_prompts_for_a_form_without_echo_or_admin_alert(monkeypatch, text):
    from app.handlers import compatibility as handler
    from app.handlers import messages

    alerts = AsyncMock()
    monkeypatch.setattr("app.admin_alerts.alert_admin_unauthorized_user", alerts)
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    application = Application.builder().token("123:offline").build()
    commands.register(application)
    messages.register(application)
    actual = _telegram_update(text)
    selected = next(item for item in application.handlers[0] if item.check_update(actual))
    assert selected.callback is handler.handle_orphan_birth_date
    update, context = _update(text), _context()

    await selected.callback(update, context)

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "Подробнее о паре" in reply and "/natal" in reply
    assert text not in reply
    assert context.user_data == {}
    alerts.assert_not_awaited()


@pytest.mark.parametrize("edited", [False, True])
def test_orphan_date_guard_preserves_ordinary_ai_routing(edited):
    from app.handlers import messages

    application = Application.builder().token("123:offline").build()
    commands.register(application)
    messages.register(application)
    actual = _telegram_update("Расскажи о событиях 09.11.1997", edited=edited)

    selected = next(item for item in application.handlers[0] if item.check_update(actual))

    assert selected.callback is (messages.handle_edited_request if edited else messages.handle_request)


def test_orphan_date_guard_leaves_active_natal_input_with_its_owner():
    from app.handlers import messages, natal_chart

    application = Application.builder().token("123:offline").build()
    commands.register(application)
    messages.register(application)
    conversation = next(
        item for item in application.handlers[0] if isinstance(item, ConversationHandler) and item.name == "natal_chart"
    )
    conversation._conversations[(98765001, 98765001)] = natal_chart.NATAL_DATE
    actual = _telegram_update("09.11.1997")

    selected = next(item for item in application.handlers[0] if item.check_update(actual))

    assert selected is conversation
    _, _, matched, _ = conversation.check_update(actual)
    assert matched.callback is natal_chart.on_date


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True])
async def test_expired_compatibility_tarot_button_preserves_active_natal_input(stored):
    from app.handlers import natal_chart

    context = _context()
    context.user_data["natal_date"] = "2003-06-30"
    token = "ab" * 8
    if stored:
        context.user_data["compatibility_tarot_context"] = {
            token: {"question": "Pair context", "lang": "en", "created_at": time.monotonic() - 3600}
        }
    application = Application.builder().token("123:offline").build()
    commands.register(application)
    conversation = next(
        item for item in application.handlers[0] if isinstance(item, ConversationHandler) and item.name == "natal_chart"
    )
    conversation._conversations[(98765001, 98765001)] = natal_chart.NATAL_DATE
    actual = _telegram_update("result", callback=f"compat_tarot:{token}")
    _, _, fallback, _ = conversation.check_update(actual)
    fake = _update()
    fake.callback_query = SimpleNamespace(
        data=f"compat_tarot:{token}", answer=AsyncMock(), message=fake.message, id="cb"
    )

    assert await fallback.callback(fake, context) is None

    assert context.user_data["natal_date"] == "2003-06-30"
    assert not state.is_in_tarot_mode(98765001)
    reply = fake.effective_message.reply_text.await_args.args[0]
    assert ("tarot" if stored else "Таро") in reply
    assert ("30 minutes" if stored else "30 минут") in reply


@pytest.mark.asyncio
async def test_failed_compatibility_tarot_entry_keeps_context_for_retry_and_natal_input(monkeypatch):
    from app.handlers import natal_chart

    token = "ab" * 8
    context = _context()
    context.user_data.update(
        natal_date="2003-06-30",
        compatibility_tarot_context={token: {"question": "Pair context", "lang": "ru", "created_at": time.monotonic()}},
    )
    start_tarot = AsyncMock(return_value=None)
    monkeypatch.setattr(cmd_tarot, "tarot_command", start_tarot)
    update = _update()
    update.callback_query = SimpleNamespace(
        data=f"compat_tarot:{token}", answer=AsyncMock(), message=update.message, id="cb"
    )

    assert await natal_chart.on_compatibility_tarot_during_natal(update, context) is None

    assert context.user_data["natal_date"] == "2003-06-30"
    assert token in context.user_data["compatibility_tarot_context"]
    start_tarot.assert_awaited_once_with(update, context, question_context="Pair context")


@pytest.mark.asyncio
async def test_denied_regular_tarot_entry_preserves_active_natal_input(monkeypatch):
    from app.handlers import natal_chart

    context = _context()
    context.user_data["natal_date"] = "2003-06-30"
    monkeypatch.setattr(cmd_tarot, "tarot_command", AsyncMock(return_value=None))

    assert await natal_chart.on_tarot_during_natal(_update("/tarot"), context) is None
    assert context.user_data["natal_date"] == "2003-06-30"


@pytest.mark.asyncio
async def test_failed_tarot_welcome_during_natal_keeps_previous_mode_and_allows_retry():
    from app.handlers import natal_chart

    token = "ab" * 8
    context = _context()
    context.user_data.update(
        natal_date="2003-06-30",
        compatibility_tarot_context={token: {"question": "Pair context", "lang": "ru", "created_at": time.monotonic()}},
    )
    context.bot.send_message.side_effect = RuntimeError("Delivery unavailable")
    previous_session = state.get_tarot_session(98765001)
    update = _update()
    update.callback_query = SimpleNamespace(
        data=f"compat_tarot:{token}", answer=AsyncMock(), message=update.message, id="cb"
    )

    assert await natal_chart.on_compatibility_tarot_during_natal(update, context) is None

    assert context.user_data["natal_date"] == "2003-06-30"
    assert token in context.user_data["compatibility_tarot_context"]
    assert not state.is_in_tarot_mode(98765001)
    assert state.get_tarot_session(98765001) == previous_session
    context.bot.send_message.side_effect = None

    assert await natal_chart.on_compatibility_tarot_during_natal(update, context) == ConversationHandler.END
    assert state.is_in_tarot_mode(98765001)
    assert "natal_date" not in context.user_data
    assert token not in context.user_data["compatibility_tarot_context"]

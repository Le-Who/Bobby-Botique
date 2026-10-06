"""Offline routing regressions for private, standalone menu words."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import Application, CallbackContext, ConversationHandler, ExtBot

from app import state
from app.handlers import commands, horoscope_subscription, messages, natal_chart

_USER_ID = 98764123
_KEY = (_USER_ID, _USER_ID)


def _update(text, *, chat_type="private", edited=False, callback=None):
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=_USER_ID, type=chat_type),
        from_user=User(id=_USER_ID, first_name="Test", is_bot=False),
        text=text,
    )
    if callback is not None:
        return Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="menu-query",
                from_user=message.from_user,
                chat_instance="menu-chat",
                message=message,
                data=callback,
            ),
        )
    return Update(update_id=1, **{("edited_message" if edited else "message"): message})


def _selected(application, update):
    for handler in application.handlers[0]:
        matched = handler.check_update(update)
        if matched:
            return handler, matched
    raise AssertionError("No registered handler accepted the update")


def _conversation(application, name):
    return next(
        handler
        for handler in application.handlers[0]
        if isinstance(handler, ConversationHandler) and handler.name == name
    )


def _callback(handler, matched):
    return matched[2].callback if isinstance(handler, ConversationHandler) else handler.callback


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("натальная", True),
        ("  НАТАЛЬНАЯ?!  ", True),
        ("  Таро!  ", True),
        ("расклад", True),
        ("  ГОРОСКОП?  ", True),
        ("/natal", False),
        ("натальная тема", False),
        ("расскажи про таро", False),
        ("расклад сил", False),
        ("гороскоп овен на сегодня", False),
        (None, False),
    ],
)
def test_menu_request_classifier_only_accepts_standalone_entries(text, expected):
    from app.handlers.menu_intents import is_standalone_menu_request

    assert is_standalone_menu_request(text) is expected


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=None))
    monkeypatch.setattr("app.repos.users.save_user_state", AsyncMock())
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(horoscope_subscription, "get_horoscope_subscription", AsyncMock(return_value=None))
    monkeypatch.setattr(natal_chart, "_natal_reports_enabled_for_handler", lambda: True)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 3600)
    monkeypatch.setattr(ExtBot, "answer_callback_query", AsyncMock(return_value=True))
    monkeypatch.setattr(ExtBot, "edit_message_text", AsyncMock())
    monkeypatch.setattr(
        ExtBot,
        "send_message",
        AsyncMock(
            return_value=Message(
                message_id=2,
                date=datetime.now(UTC),
                chat=Chat(id=_USER_ID, type="private"),
                text="Synthetic menu reply",
            )
        ),
    )
    app = Application.builder().token("123:offline").build()
    commands.register(app)
    messages.register(app)
    yield app
    handle = state._pending_persists.pop(_USER_ID, None)
    if handle is not None:
        handle.cancel()


async def _dispatch(application, update):
    update.effective_message.set_bot(application.bot)
    if update.callback_query:
        update.callback_query.set_bot(application.bot)
    context = CallbackContext.from_update(update, application)
    for group in sorted(group for group in application.handlers if group < 0):
        for handler in application.handlers[group]:
            matched = handler.check_update(update)
            if matched:
                await handler.handle_update(update, application, matched, context)
                break
    handler, matched = _selected(application, update)
    selected_callback = _callback(handler, matched)
    assert selected_callback.__name__ not in {"handle_request", "handle_edited_request", "handle_tarot_message"}
    await handler.handle_update(update, application, matched, context)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("word", "menu"),
    [
        ("натальная", "natal"),
        ("  НАТАЛЬНАЯ?!  ", "natal"),
        ("таро", "tarot"),
        ("  ТаРо?!  ", "tarot"),
        ("расклад", "tarot"),
        ("  РАСКЛАД!  ", "tarot"),
        ("гороскоп", "horoscope"),
        ("  ГОРОСКОП?!  ", "horoscope"),
    ],
)
async def test_standalone_private_words_open_their_menu(application, word, menu):
    context = await _dispatch(application, _update(word))

    sent = application.bot.send_message.await_args.kwargs
    if menu == "natal":
        assert "Натальная карта и матрица судьбы" in sent["text"]
        assert _conversation(application, "natal_chart")._conversations[_KEY] == natal_chart.NATAL_MODE
    elif menu == "tarot":
        assert "Сеанс Таро начат" in sent["text"]
        assert state.get_tarot_session(_USER_ID)["waiting_for_question"] is True
    else:
        buttons = [button.callback_data for row in sent["reply_markup"].inline_keyboard for button in row]
        assert buttons == ["horo_settings:now:today", "horo_settings:now:tomorrow", "start_horoscope"]
        assert not context.user_data.get("horo_sign")


@pytest.mark.parametrize(
    "text",
    [
        "Натальная тема интересна",
        "расскажи про таро",
        "обсудим расклад сил",
        "почему люди верят в гороскоп",
    ],
)
def test_ordinary_discussion_keeps_normal_chat_routing(application, text):
    selected, matched = _selected(application, _update(text))

    assert _callback(selected, matched) is messages.handle_request


@pytest.mark.parametrize("word", ["натальная", "таро", "расклад", "гороскоп"])
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_standalone_words_do_not_open_private_menus_in_groups(application, word, chat_type):
    selected, matched = _selected(application, _update(word, chat_type=chat_type))

    assert _callback(selected, matched) is messages.handle_request


@pytest.mark.parametrize("word", ["натальная", "таро", "расклад", "гороскоп"])
def test_edits_do_not_launch_new_menus(application, word):
    selected, matched = _selected(application, _update(word, edited=True))

    assert _callback(selected, matched) is messages.handle_edited_request


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["таро", "расклад", "гороскоп"])
@pytest.mark.parametrize("previous_state", [natal_chart.NATAL_DATE, natal_chart.NATAL_MODE])
async def test_menu_words_leave_active_natal_input(application, word, previous_state):
    natal = _conversation(application, "natal_chart")
    natal._conversations[_KEY] = previous_state
    application.user_data[_USER_ID]["natal_date"] = "Synthetic previous input"

    context = await _dispatch(application, _update(word))

    assert _KEY not in natal._conversations
    assert "natal_date" not in context.user_data
    sent = application.bot.send_message.await_args.kwargs
    if word == "гороскоп":
        assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "horo_settings:now:today"
    else:
        assert state.get_tarot_session(_USER_ID)["waiting_for_question"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["натальная", "  НАТАЛЬНАЯ?!  "])
async def test_cancelled_natal_does_not_resume_the_previous_horoscope_form(application, word):
    await _dispatch(application, _update("Menu", callback="start_horoscope"))
    await _dispatch(application, _update("Sign", callback="horo_sign:aries"))
    selected, matched = _selected(application, _update("08:45"))
    assert _callback(selected, matched) is horoscope_subscription.on_time_today_text

    await _dispatch(application, _update(word))
    await _dispatch(application, _update("Cancel", callback="natal_mode:cancel"))
    selected, matched = _selected(application, _update("Обычный вопрос после отмены"))

    assert _callback(selected, matched) is messages.handle_request
    assert "horo_sign" not in application.user_data[_USER_ID]


@pytest.mark.asyncio
async def test_horoscope_time_input_keeps_its_owner_until_a_natal_menu_entry(application):
    await _dispatch(application, _update("Menu", callback="start_horoscope"))
    await _dispatch(application, _update("Sign", callback="horo_sign:aries"))

    await _dispatch(application, _update("08:45"))
    selected, matched = _selected(application, _update("19:30"))

    assert application.user_data[_USER_ID]["horo_time_today"] == "08:45"
    assert _callback(selected, matched) is horoscope_subscription.on_time_tomorrow_text


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["таро", "расклад"])
async def test_tarot_words_leave_active_horoscope_input(application, word):
    horoscope = _conversation(application, "horoscope_subscription")
    horoscope._conversations[_KEY] = horoscope_subscription.CHOOSE_TIME_TODAY
    application.user_data[_USER_ID]["horo_sign"] = "aries"

    context = await _dispatch(application, _update(word))

    assert _KEY not in horoscope._conversations
    assert "horo_sign" not in context.user_data
    assert state.get_tarot_session(_USER_ID)["waiting_for_question"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["таро", "расклад"])
async def test_tarot_words_restart_an_active_tarot_menu_without_a_reading(application, word):
    await state.ensure_state_loaded(_USER_ID)
    state.set_tarot_mode(_USER_ID, True)
    state.set_tarot_session(_USER_ID, {"waiting_for_question": False, "history": ["Synthetic prior reading"]})

    await _dispatch(application, _update(word))

    session = state.get_tarot_session(_USER_ID)
    assert session["waiting_for_question"] is True
    assert session["history"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["натальная", "таро", "расклад", "гороскоп"])
async def test_menu_words_leave_pending_compatibility_birth_input(application, word):
    pending = {"first_date": "Synthetic previous input"}
    application.user_data[_USER_ID]["compatibility_flow"] = pending

    context = await _dispatch(application, _update(word))

    assert "compatibility_flow" not in context.user_data
    assert "first_date" not in pending
    assert "Synthetic previous input" not in application.bot.send_message.await_args.kwargs["text"]
    if word == "натальная":
        assert _conversation(application, "natal_chart")._conversations[_KEY] == natal_chart.NATAL_MODE
    elif word == "гороскоп":
        assert application.bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == (
            "horo_settings:now:today"
        )
    else:
        assert state.get_tarot_session(_USER_ID)["waiting_for_question"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["/совместимость", "совместимость", "совм", "  СОВМ?!  ", "/compatibility"])
@pytest.mark.parametrize("previous", ["natal", "horoscope", "tarot", "dates", "none"])
async def test_compatibility_entries_open_picker_and_release_previous_owner(application, entry, previous):
    if previous == "natal":
        _conversation(application, "natal_chart")._conversations[_KEY] = natal_chart.NATAL_DATE
        application.user_data[_USER_ID]["natal_date"] = "Synthetic previous input"
    elif previous == "horoscope":
        _conversation(application, "horoscope_subscription")._conversations[_KEY] = (
            horoscope_subscription.CHOOSE_TIME_TODAY
        )
        application.user_data[_USER_ID]["horo_sign"] = "aries"
    elif previous == "tarot":
        await state.ensure_state_loaded(_USER_ID)
        state.set_tarot_mode(_USER_ID, True)
        state.set_tarot_session(_USER_ID, {"waiting_for_question": True})
    elif previous == "dates":
        application.user_data[_USER_ID]["compatibility_flow"] = {"first_date": "Synthetic previous input"}

    context = await _dispatch(application, _update(entry))

    sent = application.bot.send_message.await_args.kwargs
    buttons = [button for row in sent["reply_markup"].inline_keyboard for button in row]
    assert len(buttons) == 12
    assert buttons[0].callback_data == "compat_pick:ru:0"
    assert buttons[-1].callback_data == "compat_pick:ru:11"
    assert _KEY not in _conversation(application, "natal_chart")._conversations
    assert _KEY not in _conversation(application, "horoscope_subscription")._conversations
    assert not state.is_in_tarot_mode(_USER_ID)
    assert "compatibility_flow" not in context.user_data
    assert "natal_date" not in context.user_data
    assert "horo_sign" not in context.user_data


@pytest.mark.asyncio
async def test_sign_picker_shows_short_reading_then_opens_pair_form(application, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "WEBAPP_BASE_URL", "https://bot.example.com")
    await _dispatch(application, _update("совместимость"))
    await _dispatch(application, _update("First sign", callback="compat_pick:ru:7"))
    second = application.bot.edit_message_text.await_args.kwargs
    assert "Скорпион" in second["text"]
    assert second["reply_markup"].inline_keyboard[0][0].callback_data == "compat_pair:ru:7:0"

    await _dispatch(application, _update("Second sign", callback="compat_pair:ru:7:10"))
    short = application.bot.edit_message_text.await_args.kwargs
    assert "Скорпион" in short["text"] and "Водолей" in short["text"]
    assert "Сильная сторона" in short["text"]
    details = short["reply_markup"].inline_keyboard[0][0].callback_data
    assert details == "compat_dates:compat_n7_n10"

    await _dispatch(application, _update("Details", callback=details))
    intro = application.bot.send_message.await_args.kwargs
    assert intro["reply_markup"].inline_keyboard[0][0].web_app.url.endswith("?pair=compat_n7_n10")


@pytest.mark.parametrize(
    "text", ["совместимость характеров", "обсудим совместимость", "совмещать", "совм скорпион водолей"]
)
def test_regular_phrases_are_not_compatibility_menu_entries(application, text):
    selected, matched = _selected(application, _update(text))
    assert _callback(selected, matched) is messages.handle_request

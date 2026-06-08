from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.natal_chart import (
    NATAL_CONFIRM,
    NATAL_COUNTRY,
    NATAL_DATE,
    NATAL_FOCUS,
    NATAL_PLACE,
    NATAL_TABLE,
    NATAL_TIME_VALUE,
    build_natal_chart_handler,
    clear_natal_user_data,
    natal_command,
    on_confirm,
    on_country,
    on_country_selected,
    on_date,
    on_focus,
    on_mode,
    on_place,
    on_place_missing,
    on_place_selected,
    on_table_input,
    on_time_precision,
    on_time_value,
)
from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies = []
        self.edit_text = AsyncMock()
        self.last_reply_message = None

    async def reply_text(self, text, **kwargs):
        reply = FakeMessage(text)
        self.last_reply_message = reply
        self.replies.append((text, kwargs))
        return reply


class FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = FakeMessage()
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


def make_update(text: str = ""):
    return SimpleNamespace(
        message=FakeMessage(text),
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def make_callback_update(data: str):
    return SimpleNamespace(
        message=None,
        callback_query=FakeCallbackQuery(data),
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def make_context():
    return SimpleNamespace(user_data={})


def first_callback_data(update, text_part: str) -> str:
    keyboard = update.message.replies[-1][1]["reply_markup"].inline_keyboard
    for row in keyboard:
        for button in row:
            if text_part in button.text:
                return button.callback_data
    raise AssertionError(f"No inline button containing {text_part!r}")


def edited_callback_data(context, text_part: str) -> str:
    flow_message = context.user_data["natal_flow_message"]
    keyboard = flow_message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    for row in keyboard:
        for button in row:
            if text_part in button.text:
                return button.callback_data
    raise AssertionError(f"No edited inline button containing {text_part!r}")


def fake_natal_report(url: str) -> NatalReport:
    return NatalReport(
        report_id=url.rsplit("/", 1)[-1],
        user_id=123,
        chart=ChartData(
            input_quality=InputQuality(
                time_precision=TimePrecision.UNKNOWN,
                houses_available=False,
                angles_available=False,
            ),
            planets=[
                PlanetPosition(
                    key="sun",
                    label="Солнце",
                    longitude=325.0,
                    sign="Водолей",
                    degree_in_sign=25.0,
                )
            ],
            aspects=[],
        ),
        svg="<svg></svg>",
        sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="body")],
        hosted_url=url,
    )


@pytest.mark.asyncio
async def test_natal_command_sends_mode_selection(monkeypatch):
    monkeypatch.setattr("app.handlers.natal_chart._natal_reports_enabled_for_handler", lambda: True)
    update = make_update()
    context = make_context()

    state = await natal_command(update, context)

    assert state == "NATAL_MODE"
    assert "Натальная карта строится" in update.message.replies[0][0]
    assert "natal_flow_message" in context.user_data


@pytest.mark.asyncio
async def test_natal_command_does_not_collect_birth_data_when_reports_disabled(monkeypatch):
    monkeypatch.setattr("app.handlers.natal_chart._natal_reports_enabled_for_handler", lambda: False)
    update = make_update()
    context = make_context()

    state = await natal_command(update, context)

    assert state == -1
    assert "временно недоступны" in update.message.replies[0][0]
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_step_flow_edits_single_prompt_message_for_text_steps(monkeypatch):
    monkeypatch.setattr("app.handlers.natal_chart._natal_reports_enabled_for_handler", lambda: True)
    context = make_context()
    start_update = make_update()

    assert await natal_command(start_update, context) == "NATAL_MODE"
    flow_message = context.user_data["natal_flow_message"]

    assert await on_mode(make_callback_update("natal_mode:step"), context) == NATAL_DATE
    assert await on_date(make_update("14.02.1995"), context) == "NATAL_TIME_PRECISION"

    flow_message.edit_text.assert_awaited()
    assert "Время рождения" in flow_message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_step_flow_uses_buttons_for_time_precision_and_focus():
    context = make_context()

    update = make_update("14.02.1995")
    await on_date(update, context)
    _, kwargs = update.message.replies[-1]
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert any(button.callback_data == "natal_time_precision:unknown" for row in keyboard for button in row)

    from app.natal.city_catalog import search_cities

    city = search_cities("Оде", limit=1, country_code="UA")[0]
    place_update = make_callback_update(f"natal_place:{city.geoname_id}")
    context.user_data["natal_country_code"] = "UA"

    assert await on_place_selected(place_update, context) == NATAL_FOCUS
    focus_keyboard = place_update.callback_query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert any(button.callback_data == "natal_focus:relationships" for row in focus_keyboard for button in row)


@pytest.mark.asyncio
async def test_table_mode_stores_parsed_birth_input():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Страна рождения: Украина
        Место рождения: Kyiv, Ukraine
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_CONFIRM
    assert context.user_data["natal_birth_input"].time_precision == TimePrecision.UNKNOWN
    assert context.user_data["natal_birth_input"].birth_place_country_code == "UA"


@pytest.mark.asyncio
async def test_table_mode_embeds_local_city_coordinates_before_confirmation():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Страна рождения: Украина
        Место рождения: Одесса
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_CONFIRM
    birth_input = context.user_data["natal_birth_input"]
    assert birth_input.birth_place_display_name == "Odesa, Odesa Oblast, Ukraine"
    assert birth_input.birth_place_timezone == "Europe/Kyiv"
    assert birth_input.birth_place_latitude is not None
    assert birth_input.birth_place_longitude is not None


@pytest.mark.asyncio
async def test_table_mode_uses_region_hint_for_same_name_city():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Страна рождения: США
        Место рождения: Reading, Massachusetts
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_CONFIRM
    birth_input = context.user_data["natal_birth_input"]
    assert birth_input.birth_place_display_name == "Reading, Massachusetts, United States"


@pytest.mark.asyncio
async def test_table_mode_rejects_unknown_local_city_before_confirmation():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Страна рождения: Украина
        Место рождения: Definitely Missing Natal City
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_TABLE
    assert "Город не найден" in update.message.replies[0][0]
    assert "natal_birth_input" not in context.user_data


@pytest.mark.asyncio
async def test_table_mode_requires_birth_country():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Место рождения: Kyiv, Ukraine
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_TABLE
    assert "Страна рождения" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_step_flow_exact_time_city_selection_generates_hosted_report(monkeypatch):
    captured = {}

    async def fake_create_natal_report(*, birth_input, user_id, chat_id, webhook_url):
        captured.update(
            {
                "birth_input": birth_input,
                "user_id": user_id,
                "chat_id": chat_id,
                "webhook_url": webhook_url,
            }
        )
        return fake_natal_report(f"{webhook_url}/reports/natal/exact-report-id")

    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com")
    monkeypatch.setattr("app.handlers.natal_chart.create_natal_report", fake_create_natal_report)

    context = make_context()
    update = make_callback_update("natal_mode:step")
    assert await on_mode(update, context) == NATAL_DATE

    update = make_update("14.02.1995")
    assert await on_date(update, context) == "NATAL_TIME_PRECISION"

    update = make_update("точное")
    assert await on_time_precision(update, context) == NATAL_TIME_VALUE

    update = make_update("06:30")
    assert await on_time_value(update, context) == NATAL_COUNTRY

    update = make_update("У")
    assert await on_country(update, context) == NATAL_COUNTRY
    update = make_callback_update(edited_callback_data(context, "UA"))
    assert await on_country_selected(update, context) == NATAL_PLACE

    update = make_update("Оде")
    assert await on_place(update, context) == NATAL_PLACE
    update = make_callback_update(edited_callback_data(context, "Odesa"))
    assert await on_place_selected(update, context) == NATAL_FOCUS

    update = make_update("отношения")
    assert await on_focus(update, context) == NATAL_CONFIRM
    birth_input = context.user_data["natal_birth_input"]
    assert birth_input.time_precision == TimePrecision.EXACT
    assert birth_input.birth_time == "06:30"
    assert birth_input.birth_place_display_name == "Odesa, Odesa Oblast, Ukraine"
    assert birth_input.birth_place_timezone == "Europe/Kyiv"

    update = make_callback_update("natal_confirm:yes")
    assert await on_confirm(update, context) == -1

    assert captured["user_id"] == 123
    assert captured["chat_id"] == 456
    assert captured["webhook_url"] == "https://bot.example.com"
    assert captured["birth_input"].birth_place_latitude is not None
    assert update.callback_query.edit_message_text.await_args.args[0] == "Готово: https://bot.example.com/reports/natal/exact-report-id"
    assert context.user_data == {}


@pytest.mark.asyncio
async def test_step_flow_unknown_time_city_selection_generates_limited_report(monkeypatch):
    captured = {}

    async def fake_create_natal_report(*, birth_input, user_id, chat_id, webhook_url):
        captured["birth_input"] = birth_input
        return fake_natal_report(f"{webhook_url}/reports/natal/unknown-report-id")

    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com")
    monkeypatch.setattr("app.handlers.natal_chart.create_natal_report", fake_create_natal_report)

    context = make_context()
    assert await on_date(make_update("14.02.1995"), context) == "NATAL_TIME_PRECISION"
    assert await on_time_precision(make_update("неизвестно"), context) == NATAL_COUNTRY

    country_update = make_update("У")
    assert await on_country(country_update, context) == NATAL_COUNTRY
    assert await on_country_selected(make_callback_update(edited_callback_data(context, "UA")), context) == NATAL_PLACE

    city_update = make_update("Оде")
    assert await on_place(city_update, context) == NATAL_PLACE
    assert await on_place_selected(make_callback_update(edited_callback_data(context, "Odesa")), context) == NATAL_FOCUS

    focus_update = make_update("общий")
    assert await on_focus(focus_update, context) == NATAL_CONFIRM
    confirmation_text = context.user_data["natal_flow_message"].edit_text.await_args.args[0]
    assert "Без точного времени" in confirmation_text

    confirm_update = make_callback_update("natal_confirm:yes")
    assert await on_confirm(confirm_update, context) == -1

    birth_input = captured["birth_input"]
    assert birth_input.time_precision == TimePrecision.UNKNOWN
    assert birth_input.birth_time is None
    assert birth_input.birth_place_timezone == "Europe/Kyiv"
    assert confirm_update.callback_query.edit_message_text.await_args.args[0].endswith("/reports/natal/unknown-report-id")


def test_cancel_clears_natal_keys_from_user_data():
    data = {"natal_birth_input": object(), "natal_mode": "table", "other": 1}

    clear_natal_user_data(data)

    assert data == {"other": 1}


@pytest.mark.asyncio
async def test_unknown_time_skips_time_value_prompt():
    update = make_update("неизвестно")
    context = make_context()

    state = await on_time_precision(update, context)

    assert state == NATAL_COUNTRY
    assert context.user_data["natal_time_precision"] == TimePrecision.UNKNOWN


@pytest.mark.asyncio
async def test_invalid_time_precision_does_not_default_to_range():
    update = make_update("утром")
    context = make_context()

    state = await on_time_precision(update, context)

    assert state == "NATAL_TIME_PRECISION"
    assert "Время рождения известно?" in update.message.replies[0][0]
    keyboard = update.message.replies[0][1]["reply_markup"].inline_keyboard
    assert any(button.callback_data == "natal_time_precision:range" for row in keyboard for button in row)
    assert "natal_time_precision" not in context.user_data


@pytest.mark.asyncio
async def test_step_flow_normalizes_birth_input_date_and_focus():
    update = make_update("отношения")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.UNKNOWN,
            "natal_country_code": "UA",
            "natal_country": "Ukraine (UA)",
            "natal_place": "Kyiv, Ukraine",
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_CONFIRM
    assert context.user_data["natal_birth_input"].birth_date == "1995-02-14"
    assert context.user_data["natal_birth_input"].focus == "relationships"


@pytest.mark.asyncio
async def test_step_flow_rejects_overnight_time_range_before_confirmation():
    update = make_update("общий")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.RANGE,
            "natal_time_value": "23:30-01:30",
            "natal_country_code": "UA",
            "natal_country": "Ukraine (UA)",
            "natal_place": "Kyiv, Ukraine",
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_TIME_VALUE
    assert "Диапазон времени" in update.message.replies[0][0]
    assert "natal_birth_input" not in context.user_data


@pytest.mark.asyncio
async def test_step_flow_rejects_incomplete_time_range_before_confirmation():
    update = make_update("общий")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.RANGE,
            "natal_time_value": "около 06:00",
            "natal_country_code": "UA",
            "natal_country": "Ukraine (UA)",
            "natal_place": "Kyiv, Ukraine",
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_TIME_VALUE
    assert "Диапазон времени" in update.message.replies[0][0]
    assert "natal_birth_input" not in context.user_data


@pytest.mark.asyncio
async def test_step_flow_rejects_approximate_time_without_value_before_confirmation():
    update = make_update("общий")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.APPROXIMATE,
            "natal_time_value": "примерно утром",
            "natal_country_code": "UA",
            "natal_country": "Ukraine (UA)",
            "natal_place": "Kyiv, Ukraine",
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_TIME_VALUE
    assert "примерное время" in update.message.replies[0][0]
    assert "natal_birth_input" not in context.user_data


def test_conversation_handler_accepts_text_intent_entrypoint():
    handler = build_natal_chart_handler()

    assert len(handler.entry_points) >= 2


@pytest.mark.asyncio
async def test_step_flow_embeds_selected_country_code_in_birth_input():
    from app.natal.city_catalog import search_cities

    city = search_cities("Оде", limit=1, country_code="UA")[0]
    update = make_update("отношения")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.UNKNOWN,
            "natal_country_code": "UA",
            "natal_place": city.display_name,
            "natal_place_data": {
                "geoname_id": city.geoname_id,
                "display_name": city.display_name,
                "latitude": city.latitude,
                "longitude": city.longitude,
                "timezone": city.timezone,
            },
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_CONFIRM
    birth_input = context.user_data["natal_birth_input"]
    assert birth_input.birth_place_country_code == "UA"
    assert birth_input.birth_place_timezone == "Europe/Kyiv"


@pytest.mark.asyncio
async def test_country_prefix_returns_country_suggestions():
    update = make_update("У")
    context = make_context()

    state = await on_country(update, context)

    assert state == NATAL_COUNTRY
    reply_text, kwargs = update.message.replies[0]
    assert "Выберите страну" in reply_text
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert any("Ukraine" in button.text or "UA" in button.text for row in keyboard for button in row)


@pytest.mark.asyncio
async def test_country_selection_stores_country_and_asks_city():
    update = make_callback_update("natal_country:UA")
    context = make_context()

    state = await on_country_selected(update, context)

    assert state == NATAL_PLACE
    assert context.user_data["natal_country_code"] == "UA"
    update.callback_query.edit_message_text.assert_awaited()
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "Город рождения" in text


@pytest.mark.asyncio
async def test_place_prefix_returns_city_suggestions_filtered_by_country():
    update = make_update("Оде")
    context = make_context()
    context.user_data["natal_country_code"] = "UA"

    state = await on_place(update, context)

    assert state == NATAL_PLACE
    reply_text, kwargs = update.message.replies[0]
    assert "Выберите город" in reply_text
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert any("Odesa" in button.text or "Odessa" in button.text for row in keyboard for button in row)
    assert any("Нет в списке" in button.text for row in keyboard for button in row)


@pytest.mark.asyncio
async def test_place_prefix_requires_selected_country():
    update = make_update("Оде")
    context = make_context()

    state = await on_place(update, context)

    assert state == NATAL_COUNTRY
    assert "Сначала выберите страну" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_place_selection_stores_city_coordinates_and_asks_focus():
    from app.natal.city_catalog import search_cities

    city = search_cities("Оде", limit=1)[0]
    update = make_callback_update(f"natal_place:{city.geoname_id}")
    context = make_context()
    context.user_data["natal_country_code"] = "UA"

    state = await on_place_selected(update, context)

    assert state == NATAL_FOCUS
    assert context.user_data["natal_place"] == city.display_name
    assert context.user_data["natal_place_data"]["timezone"] == "Europe/Kyiv"
    update.callback_query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_place_selection_requires_selected_country():
    from app.natal.city_catalog import search_cities

    city = search_cities("Оде", limit=1, country_code="UA")[0]
    update = make_callback_update(f"natal_place:{city.geoname_id}")
    context = make_context()

    state = await on_place_selected(update, context)

    assert state == NATAL_COUNTRY
    assert "Сначала выберите страну" in update.callback_query.edit_message_text.await_args.args[0]
    assert "natal_place_data" not in context.user_data


@pytest.mark.asyncio
async def test_place_selection_rejects_city_outside_selected_country():
    from app.natal.city_catalog import search_cities

    city = search_cities("Ottawa", limit=1, country_code="CA")[0]
    update = make_callback_update(f"natal_place:{city.geoname_id}")
    context = make_context()
    context.user_data["natal_country_code"] = "UA"

    state = await on_place_selected(update, context)

    assert state == NATAL_PLACE
    assert "natal_place_data" not in context.user_data
    update.callback_query.edit_message_text.assert_awaited()
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "выбранной стране" in text


@pytest.mark.asyncio
async def test_missing_place_callback_asks_for_nearest_large_city():
    update = make_callback_update("natal_place_missing")
    context = make_context()

    state = await on_place_missing(update, context)

    assert state == NATAL_PLACE
    update.callback_query.edit_message_text.assert_awaited()
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "ближайший крупный город" in text

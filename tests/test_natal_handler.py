from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.natal_chart import (
    NATAL_CONFIRM,
    NATAL_COUNTRY,
    NATAL_FOCUS,
    NATAL_PLACE,
    NATAL_TABLE,
    build_natal_chart_handler,
    clear_natal_user_data,
    natal_command,
    on_country,
    on_country_selected,
    on_focus,
    on_place,
    on_place_missing,
    on_place_selected,
    on_table_input,
    on_time_precision,
)
from app.natal.models import TimePrecision


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(edit_text=AsyncMock())


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


@pytest.mark.asyncio
async def test_natal_command_sends_mode_selection():
    update = make_update()
    context = make_context()

    state = await natal_command(update, context)

    assert state == "NATAL_MODE"
    assert "Натальная карта строится" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_table_mode_stores_parsed_birth_input():
    update = make_update(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Место рождения: Kyiv, Ukraine
        """
    )
    context = make_context()

    state = await on_table_input(update, context)

    assert state == NATAL_CONFIRM
    assert context.user_data["natal_birth_input"].time_precision == TimePrecision.UNKNOWN


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
async def test_step_flow_normalizes_birth_input_date_and_focus():
    update = make_update("отношения")
    context = make_context()
    context.user_data.update(
        {
            "natal_date": "14.02.1995",
            "natal_time_precision": TimePrecision.UNKNOWN,
            "natal_place": "Kyiv, Ukraine",
        }
    )

    state = await on_focus(update, context)

    assert state == NATAL_CONFIRM
    assert context.user_data["natal_birth_input"].birth_date == "1995-02-14"
    assert context.user_data["natal_birth_input"].focus == "relationships"


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
async def test_place_selection_stores_city_coordinates_and_asks_focus():
    from app.natal.city_catalog import search_cities

    city = search_cities("Оде", limit=1)[0]
    update = make_callback_update(f"natal_place:{city.geoname_id}")
    context = make_context()

    state = await on_place_selected(update, context)

    assert state == NATAL_FOCUS
    assert context.user_data["natal_place"] == city.display_name
    assert context.user_data["natal_place_data"]["timezone"] == "Europe/Kyiv"
    update.callback_query.edit_message_text.assert_awaited()


@pytest.mark.asyncio
async def test_missing_place_callback_asks_for_nearest_large_city():
    update = make_callback_update("natal_place_missing")
    context = make_context()

    state = await on_place_missing(update, context)

    assert state == NATAL_PLACE
    update.callback_query.edit_message_text.assert_awaited()
    text = update.callback_query.edit_message_text.await_args.args[0]
    assert "ближайший крупный город" in text

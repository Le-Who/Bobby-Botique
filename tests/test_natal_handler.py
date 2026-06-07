from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.natal_chart import (
    NATAL_CONFIRM,
    NATAL_PLACE,
    NATAL_TABLE,
    build_natal_chart_handler,
    clear_natal_user_data,
    natal_command,
    on_focus,
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


def make_update(text: str = ""):
    return SimpleNamespace(
        message=FakeMessage(text),
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

    assert state == NATAL_PLACE
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

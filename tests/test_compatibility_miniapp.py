from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.natal.models import TimePrecision
from app.web import quart_app
from tests.factories import make_valid_init_data

_TOKEN = "1234567890:dummy-token-for-tests-only"
_PAIR = "compat_m7_f10"


def _payload(**first):
    return {
        "pair": _PAIR,
        "first": {"birth_date": "1995-06-15", "time_precision": "unknown", **first},
        "second": {"birth_date": "1989-10-20", "time_precision": "unknown"},
    }


def _headers():
    return {"Authorization": f"tma {make_valid_init_data(_TOKEN, user_id=777)}"}


def test_date_only_form_does_not_require_place_or_time():
    from app.natal.compatibility_input import parse_pair_input

    value = parse_pair_input(_payload())
    assert value.first.time_precision == TimePrecision.UNKNOWN
    assert value.first.birth_place == ""
    assert value.second.birth_time is None
    assert value.pair.first.sign_index == 7


def test_exact_time_uses_catalog_timezone_and_ignores_client_coordinates():
    from app.natal.compatibility_input import parse_pair_input

    value = parse_pair_input(
        _payload(
            time_precision="exact",
            birth_time="12:30",
            country_code="UA",
            city_geoname_id="703448",
            birth_place_timezone="UTC",
            birth_place_latitude=0,
            birth_place_longitude=0,
        )
    )
    assert value.first.birth_time == "12:30"
    assert value.first.birth_place_timezone == "Europe/Kyiv"
    assert value.first.birth_place_latitude > 50
    assert value.second.time_precision == TimePrecision.UNKNOWN


@pytest.mark.parametrize(
    "first",
    [
        {"time_precision": "exact", "birth_time": "12:30"},
        {"time_precision": "exact", "country_code": "UA", "city_geoname_id": "703448"},
        {"time_precision": "invented"},
        {"birth_date": "2001-02-29"},
        {"birth_date": "2099-01-01"},
        {"time_precision": "exact", "birth_time": "25:30", "country_code": "UA", "city_geoname_id": "703448"},
        {"country_code": "RU", "city_geoname_id": "703448"},
    ],
)
def test_invalid_partner_data_is_rejected(first):
    from app.natal.compatibility_input import parse_pair_input

    with pytest.raises(ValueError):
        parse_pair_input(_payload(**first))


@pytest.mark.parametrize("payload", [[], {"pair": _PAIR, "first": [], "second": {}}, {"pair": "compat_t_m7_f10"}])
def test_invalid_pair_form_is_rejected(payload):
    from app.natal.compatibility_input import parse_pair_input

    with pytest.raises(ValueError):
        parse_pair_input(payload)


@pytest.fixture
def web_settings(monkeypatch):
    settings = SimpleNamespace(TELEGRAM_BOT_TOKEN=_TOKEN, WEBAPP_BASE_URL="https://bot.example.com")
    monkeypatch.setattr("app.web_miniapp.settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    quart_app.config["TESTING"] = True
    return settings


@pytest.mark.asyncio
async def test_pair_form_is_public_shell_with_validated_sign_context(web_settings):
    response = await quart_app.test_client().get(f"/webapp/compatibility-form?pair={_PAIR}")
    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "Первый партнёр" in body and "Второй партнёр" in body
    assert "Рассчитать совместимость" in body
    assert "compatibility-form.js" in body
    bad = await quart_app.test_client().get("/webapp/compatibility-form?pair=invalid")
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_unsigned_and_revoked_form_submissions_are_rejected(web_settings, monkeypatch):
    client = quart_app.test_client()
    unsigned = await client.post("/webapp/api/compatibility/submit", json=_payload())
    assert unsigned.status_code == 401
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    revoked = await client.post("/webapp/api/compatibility/submit", json=_payload(), headers=_headers())
    assert revoked.status_code == 403


@pytest.mark.asyncio
async def test_form_delivers_only_to_signed_private_chat_and_stores_derived_tarot(web_settings, monkeypatch):
    from app import web_compatibility

    bot = SimpleNamespace(send_message=AsyncMock())
    user_data = {777: {}}
    monkeypatch.setattr(web_compatibility, "get_bot", lambda: bot)
    monkeypatch.setattr(web_compatibility, "get_application", lambda: SimpleNamespace(user_data=user_data))
    tasks = []

    def schedule(coro, **kwargs):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(web_compatibility, "submit_task", schedule)
    payload = _payload()
    payload["user_id"] = 999
    payload["chat_id"] = -999
    response = await quart_app.test_client().post("/webapp/api/compatibility/submit", json=payload, headers=_headers())
    assert response.status_code == 202
    assert await response.get_json() == {"ok": True, "status": "accepted"}
    await asyncio.gather(*tasks)
    sent = bot.send_message.await_args.kwargs
    assert sent["chat_id"] == 777
    assert "Совместимость" in sent["text"]
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data.startswith("compat_tarot:")
    stored = next(iter(user_data[777]["compatibility_tarot_context"].values()))
    assert "1995-06-15" not in stored["question"]
    assert "1989-10-20" not in stored["question"]


@pytest.mark.asyncio
async def test_invalid_form_does_not_schedule_or_echo_birth_data(web_settings, monkeypatch):
    from app import web_compatibility

    monkeypatch.setattr(web_compatibility, "get_bot", lambda: SimpleNamespace())
    scheduled = []
    monkeypatch.setattr(web_compatibility, "submit_task", lambda coro, **kwargs: scheduled.append(coro))
    response = await quart_app.test_client().post(
        "/webapp/api/compatibility/submit", json=_payload(birth_date="2099-01-01"), headers=_headers()
    )
    assert response.status_code == 400
    assert not scheduled
    assert "2099" not in await response.get_data(as_text=True)


@pytest.mark.asyncio
async def test_server_validation_names_the_invalid_field(web_settings):
    response = await quart_app.test_client().post(
        "/webapp/api/compatibility/submit", json=_payload(birth_date="2099-01-01"), headers=_headers()
    )
    detail = (await response.get_json())["detail"]
    assert "дату рождения" in detail
    assert "Укажите время" not in detail
    assert "Выберите страну" not in detail


@pytest.mark.parametrize("birth_date,birth_time", [("2024-03-10", "02:30"), ("2024-11-03", "01:30")])
@pytest.mark.asyncio
async def test_dst_gap_and_fold_are_rejected_before_acceptance(web_settings, monkeypatch, birth_date, birth_time):
    from app import web_compatibility

    scheduled = []
    monkeypatch.setattr(web_compatibility, "get_bot", lambda: SimpleNamespace())
    monkeypatch.setattr(web_compatibility, "get_application", lambda: SimpleNamespace())
    monkeypatch.setattr(web_compatibility, "submit_task", lambda coro, **kwargs: scheduled.append(coro))
    response = await quart_app.test_client().post(
        "/webapp/api/compatibility/submit",
        headers=_headers(),
        json=_payload(
            birth_date=birth_date,
            time_precision="exact",
            birth_time=birth_time,
            country_code="US",
            city_geoname_id="5128581",
        ),
    )
    for coro in scheduled:
        coro.close()
    assert response.status_code == 400
    assert not scheduled
    detail = (await response.get_json())["detail"]
    assert "перевод" in detail.lower()
    assert birth_date not in detail and birth_time not in detail


@pytest.mark.asyncio
async def test_private_start_opens_one_pair_form_instead_of_collecting_dates(web_settings, monkeypatch):
    from app import state
    from app.handlers.compatibility import start_compatibility

    monkeypatch.setattr(state, "ensure_state_loaded", AsyncMock())
    monkeypatch.setattr(state, "_schedule_persist", lambda _: None)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(type="private"),
        effective_user=SimpleNamespace(id=777),
    )
    context = SimpleNamespace(user_data={})
    await start_compatibility(update, context, _PAIR)
    sent = message.reply_text.await_args
    buttons = sent.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1 and len(buttons[0]) == 1
    assert buttons[0][0].web_app.url == f"https://bot.example.com/webapp/compatibility-form?pair={_PAIR}"
    assert "compatibility_flow" not in context.user_data
    assert "Время рождения можно пропустить" in sent.args[0]
    assert "Расчёт будет без домов" not in sent.args[0]


@pytest.mark.asyncio
async def test_mixed_time_reading_keeps_known_ascendant_and_per_partner_limits():
    from app.natal.compatibility_input import parse_pair_input
    from app.natal.compatibility_reading import build_pair_reading

    value = parse_pair_input(
        _payload(time_precision="exact", birth_time="12:30", country_code="UA", city_geoname_id="703448")
    )
    reading = await build_pair_reading(value)
    assert "Асцендент:" in reading.html
    assert "Партнёр 2: время не указано" in reading.html
    assert "Партнёр 1: время не указано" not in reading.html
    assert "Основные аспекты между картами" in reading.html
    assert "дом" in reading.html
    for raw in ("1995-06-15", "1989-10-20", "12:30", "Kyiv", "Киев"):
        assert raw not in reading.html and raw not in reading.tarot_context


@pytest.mark.asyncio
async def test_direct_known_time_without_city_cannot_silently_become_date_only():
    from app.natal.compatibility_input import PairInput, parse_pair_input
    from app.natal.compatibility_reading import build_pair_reading
    from app.natal.geocoding import GeocodingError

    value = parse_pair_input(_payload())
    first = value.first.model_copy(update={"time_precision": TimePrecision.EXACT, "birth_time": "12:30"})
    with pytest.raises(GeocodingError):
        await build_pair_reading(PairInput(value.pair, first, value.second))


@pytest.mark.asyncio
async def test_city_search_respects_selected_country_and_requires_signed_user(web_settings):
    client = quart_app.test_client()
    unsigned = await client.get("/webapp/api/compatibility/cities?q=Kyiv&country=UA")
    assert unsigned.status_code == 401
    response = await client.get("/webapp/api/compatibility/cities?q=Kyiv&country=UA", headers=_headers())
    assert response.status_code == 200
    items = (await response.get_json())["items"]
    assert items and items[0]["id"] == "703448"
    other = await client.get("/webapp/api/compatibility/cities?q=Kyiv&country=RU", headers=_headers())
    assert all(item["id"] != "703448" for item in (await other.get_json())["items"])


@pytest.mark.asyncio
async def test_revoked_access_before_background_delivery_sends_no_private_result(web_settings, monkeypatch):
    from app import web_compatibility
    from app.natal.compatibility_input import parse_pair_input

    bot = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=False))
    await web_compatibility._send_pair_result(
        bot, SimpleNamespace(user_data={777: {}}), 777, parse_pair_input(_payload())
    )
    assert bot.send_message.await_count == 0

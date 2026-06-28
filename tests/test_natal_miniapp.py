from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.natal.models import (
    BirthInput,
    ChartData,
    InputQuality,
    NatalReport,
    PlanetPosition,
    ReportSection,
    ReportType,
    TimePrecision,
)
from app.web import quart_app
from tests.factories import make_valid_init_data

_BOT_TOKEN = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _auth_headers(user_id: int = 777) -> dict[str, str]:
    init_data = make_valid_init_data(_BOT_TOKEN, user_id=user_id)
    return {"Authorization": f"tma {init_data}"}


def _fake_report(url: str) -> NatalReport:
    return NatalReport(
        report_id=url.rsplit("/", 1)[-1],
        user_id=777,
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


@pytest.fixture(autouse=True)
def natal_miniapp_settings(monkeypatch):
    settings = SimpleNamespace(
        TELEGRAM_BOT_TOKEN=_BOT_TOKEN,
        WEBAPP_BASE_URL="https://bot.example.com",
        NATAL_REPORTS_ENABLED=True,
    )
    monkeypatch.setattr("app.web_miniapp.settings", settings, raising=False)
    monkeypatch.setattr("app.config.settings", settings, raising=False)
    monkeypatch.setenv("WEBHOOK_URL", "https://bot.example.com")
    quart_app.config["TESTING"] = True
    return settings


@pytest.mark.asyncio
async def test_natal_form_page_returns_miniapp_shell():
    client = quart_app.test_client()

    response = await client.get("/webapp/natal-form")

    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "telegram-web-app.js" in body
    assert 'id="natal-form"' in body
    assert 'data-api="/webapp/api/natal/submit"' in body
    assert "Дата рождения" in body
    assert 'data-group="report_type"' in body
    assert "Натал + матрица" in body
    assert "Только матрица" in body
    assert "Рекомендуем" in body
    assert "Лучший выбор для первого разбора" in body
    assert 'id="report-type-panel"' in body
    assert 'id="cancel-button"' in body
    assert 'id="progress-meter"' in body
    assert 'id="progress-text"' in body
    assert "Шаг 1 из 4" in body
    assert "Ваш разбор" in body
    assert "Осталось заполнить" in body
    assert "Фокус разбора" in body
    assert "Создаём разбор рождения" in body
    assert "Окно можно закрывать." in body
    assert "Ответ будет отправлен новым сообщением" in body
    assert ".layout[hidden]" in body
    assert "scrollIntoView({ behavior: 'smooth'" in body
    assert "advanceToNextItem" in body
    assert body.index("Дата рождения") < body.index("Место рождения")
    assert body.index("Место рождения") < body.index("Фокус разбора")
    assert body.index("Фокус разбора") < body.index("Что построить")
    assert body.index("Что построить") < body.index('data-group="report_type"')
    assert body.index("const response = await fetch") < body.index("showAcceptedState();")


@pytest.mark.asyncio
async def test_natal_form_keeps_questionnaire_lightweight():
    client = quart_app.test_client()

    response = await client.get("/webapp/natal-form")

    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "Что будет в отчёте" not in body
    assert "Расчёт строится локально" not in body
    assert "Выберите формат и дату рождения" not in body
    assert "Введите дату, время и место" in body
    assert "Тип разбора" not in body
    assert body.index("Дата рождения") < body.index("Что построить")


@pytest.mark.asyncio
async def test_natal_submit_requires_webapp_auth():
    client = quart_app.test_client()

    response = await client.post("/webapp/api/natal/submit", json={})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_natal_submit_accepts_form_before_report_is_ready(monkeypatch):
    sent = {}
    scheduled = {}

    async def fake_create_natal_report(*, birth_input, user_id, chat_id, webhook_url):
        assert isinstance(birth_input, BirthInput)
        sent["birth_input"] = birth_input
        sent["user_id"] = user_id
        sent["chat_id"] = chat_id
        sent["webhook_url"] = webhook_url
        return _fake_report(f"{webhook_url}/reports/natal/webapp-report-id")

    def fake_submit_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace(done=lambda: False)

    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())

    monkeypatch.setattr("app.web_miniapp.create_natal_report", fake_create_natal_report)
    monkeypatch.setattr("app.web_miniapp.submit_task", fake_submit_task, raising=False)
    monkeypatch.setattr("app.web_miniapp.get_bot", lambda: bot)
    monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", AsyncMock(return_value=None))

    client = quart_app.test_client()
    response = await client.post(
        "/webapp/api/natal/submit",
        headers=_auth_headers(user_id=777),
        json={
            "birth_date": "1997-11-09",
            "time_precision": "exact",
            "birth_time": "03:00",
            "country_code": "UA",
            "city_geoname_id": "698740",
            "focus": "psychology",
        },
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "accepted"
    assert "hosted_url" not in payload
    assert "coro" in scheduled
    bot.send_message.assert_not_awaited()

    await scheduled["coro"]

    assert sent["user_id"] == 777
    assert sent["chat_id"] == 777
    assert sent["webhook_url"] == "https://bot.example.com"
    assert sent["birth_input"].birth_place_country_code == "UA"
    assert sent["birth_input"].birth_place_geoname_id == "698740"
    assert sent["birth_input"].birth_place_timezone == "Europe/Kyiv"
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 777
    assert "Натальная карта готова" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_natal_submit_matrix_only_requires_only_birth_date(monkeypatch):
    sent = {}
    scheduled = {}

    async def fake_create_natal_report(*, birth_input, user_id, chat_id, webhook_url):
        sent["birth_input"] = birth_input
        sent["user_id"] = user_id
        sent["chat_id"] = chat_id
        return _fake_report(f"{webhook_url}/reports/natal/matrix-report-id")

    def fake_submit_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace(done=lambda: False)

    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())

    monkeypatch.setattr("app.web_miniapp.create_natal_report", fake_create_natal_report)
    monkeypatch.setattr("app.web_miniapp.submit_task", fake_submit_task, raising=False)
    monkeypatch.setattr("app.web_miniapp.get_bot", lambda: bot)
    monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", AsyncMock(return_value=None))

    client = quart_app.test_client()
    response = await client.post(
        "/webapp/api/natal/submit",
        headers=_auth_headers(user_id=777),
        json={
            "birth_date": "1997-11-09",
            "report_type": "destiny_matrix",
            "focus": "psychology",
        },
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["ok"] is True
    assert "coro" in scheduled

    await scheduled["coro"]

    assert sent["birth_input"].report_type == ReportType.DESTINY_MATRIX
    assert sent["birth_input"].birth_date == "1997-11-09"
    assert sent["birth_input"].birth_place == ""
    assert bot.send_message.await_args.kwargs["chat_id"] == 777
    assert "Матрица судьбы готова" in bot.send_message.await_args.kwargs["text"]

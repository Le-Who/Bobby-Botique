"""Queued natal work rechecks bot access at provider, write and delivery boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from app.natal import service
from app.natal.models import ChartData, InputQuality, ReportType, TimePrecision
from app.web import quart_app
from app.web_miniapp import _build_and_send_natal_report
from tests.factories import make_valid_init_data

_publish_telegraph = service._try_publish_telegraph


@pytest.fixture
def queued_natal(monkeypatch):
    # Boundary tests need report shape, never personal birth values.
    birth = SimpleNamespace(report_type=ReportType.NATAL, language="ru", focus="general")
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN, houses_available=False, angles_available=False
        ),
        planets=[],
        aspects=[],
    )
    access = AsyncMock(return_value=True)
    resolve = AsyncMock(return_value=object())
    interpret = AsyncMock(return_value=[])
    save = AsyncMock()
    publish = AsyncMock(return_value="https://telegra.ph/synthetic")
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    settings = SimpleNamespace(
        TELEGRAM_BOT_TOKEN="1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        WEBAPP_BASE_URL="https://bot.example.com",
        NATAL_REPORTS_ENABLED=True,
    )
    monkeypatch.setattr("app.web_miniapp.settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.repos.users.is_authorized", access)
    monkeypatch.setattr("app.web_miniapp.get_bot", lambda: bot)
    monkeypatch.setattr("app.web_miniapp._birth_input_from_natal_payload", lambda payload: birth)
    monkeypatch.setattr("app.web_miniapp.create_natal_report", service.create_natal_report)
    monkeypatch.setattr("app.web_miniapp.natal_result_caption", lambda *args: "Synthetic result")
    monkeypatch.setattr("app.web_miniapp.natal_result_keyboard", lambda *args: None)
    monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "resolve_birth_data", resolve)
    monkeypatch.setattr(service, "calculate_chart", AsyncMock(return_value=chart))
    monkeypatch.setattr(service, "generate_interpretation", interpret)
    monkeypatch.setattr(service, "render_chart_svg", lambda chart: "<svg></svg>")
    monkeypatch.setattr(service, "save_report", save)
    monkeypatch.setattr(service, "_telegraph_publication_enabled", lambda: True)
    monkeypatch.setattr(service, "_try_publish_telegraph", publish)
    quart_app.config["TESTING"] = True
    return SimpleNamespace(
        birth=birth,
        access=access,
        resolve=resolve,
        interpret=interpret,
        save=save,
        publish=publish,
        bot=bot,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_access_revoked_after_http_acceptance_stops_all_queued_effects(monkeypatch, queued_natal):
    queued = []
    monkeypatch.setattr("app.web_miniapp.submit_task", queued.append)
    token = make_valid_init_data(queued_natal.settings.TELEGRAM_BOT_TOKEN, user_id=777)
    response = await quart_app.test_client().post(
        "/webapp/api/natal/submit", headers={"Authorization": f"tma {token}"}, json={}
    )
    assert response.status_code == 200
    assert await response.get_json() == {"ok": True, "status": "accepted"}
    assert len(queued) == 1
    queued_natal.access.return_value = False
    await queued[0]
    queued_natal.resolve.assert_not_awaited()
    queued_natal.interpret.assert_not_awaited()
    queued_natal.save.assert_not_awaited()
    queued_natal.publish.assert_not_awaited()
    queued_natal.bot.send_message.assert_not_awaited()
    queued_natal.bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["resolve", "interpret", "save", "cover"])
async def test_revocation_during_await_stops_every_later_effect(monkeypatch, queued_natal, boundary):
    started = asyncio.Event()
    release = asyncio.Event()
    dependency = getattr(queued_natal, boundary, None)

    async def suspended(*args, **kwargs):
        started.set()
        await release.wait()
        return [] if boundary == "interpret" else None

    if boundary == "cover":
        monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", suspended)
    else:
        dependency.side_effect = suspended
    task = asyncio.create_task(
        _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        queued_natal.access.return_value = False
        release.set()
        await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if boundary == "resolve":
        queued_natal.interpret.assert_not_awaited()
    if boundary in {"resolve", "interpret"}:
        queued_natal.save.assert_not_awaited()
    if boundary != "cover":
        queued_natal.publish.assert_not_awaited()
    queued_natal.bot.send_message.assert_not_awaited()
    queued_natal.bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_failure_logs_only_failure_type(queued_natal, caplog):
    queued_natal.interpret.side_effect = RuntimeError("private-input-sentinel")
    await _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    assert "private-input-sentinel" not in caplog.text
    queued_natal.save.assert_not_awaited()
    assert queued_natal.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_failed_natal_error_notification_logs_only_exception_type(queued_natal, caplog):
    queued_natal.interpret.side_effect = RuntimeError("provider-private-sentinel")
    queued_natal.bot.send_message.side_effect = TelegramError("notification-private-sentinel")
    await _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    assert "provider-private-sentinel" not in caplog.text
    assert "notification-private-sentinel" not in caplog.text
    assert "type=TelegramError" in caplog.text
    queued_natal.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_natal_cover_falls_back_without_raw_exception_log(monkeypatch, queued_natal, caplog):
    monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", AsyncMock(return_value=b"synthetic-cover"))
    queued_natal.bot.send_photo.side_effect = TelegramError("cover-private-sentinel")
    await _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    assert "cover-private-sentinel" not in caplog.text
    assert "type=TelegramError" in caplog.text
    assert queued_natal.bot.send_photo.await_count == 1
    assert queued_natal.bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_public_mirror_failure_keeps_private_report_without_raw_exception_log(monkeypatch, queued_natal, caplog):
    monkeypatch.setattr(service, "_try_publish_telegraph", _publish_telegraph)
    monkeypatch.setattr(service, "build_telegraph_markdown", lambda report: "Synthetic mirror")
    monkeypatch.setattr(
        service, "create_telegraph_page_from_markdown", AsyncMock(side_effect=RuntimeError("mirror-private-sentinel"))
    )
    await _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    assert "mirror-private-sentinel" not in caplog.text
    assert "type=RuntimeError" in caplog.text
    assert queued_natal.save.await_count == 1
    assert queued_natal.save.await_args.args[0].telegraph_url is None
    assert queued_natal.bot.send_message.await_count == 1

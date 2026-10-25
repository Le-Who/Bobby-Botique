"""Old queued reports must not become current after account recreation."""

import asyncio

import pytest

from app.natal import service
from app.natal.private_work import capture_natal_epoch
from app.web import quart_app
from app.web_miniapp import _build_and_send_natal_report
from tests.factories import make_valid_init_data
from tests.test_natal_queued_access import queued_natal  # noqa: F401


@pytest.mark.asyncio
async def test_queued_natal_report_cannot_cross_account_generation(monkeypatch, queued_natal):  # noqa: F811
    # Simulate the database boundary only: account erasure/recreation preserves
    # Telegram user ID and authorization, but allocates a different durable epoch.
    # No raw birth values, real database, providers or Telegram calls are used.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def interpret(*args, **kwargs):
        entered.set()
        await release.wait()
        return []

    queued_natal.interpret.side_effect = interpret
    task = asyncio.create_task(
        _build_and_send_natal_report(queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com")
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued_natal.privacy.epoch = 200
        # Authorization is true again for the recreated account. A boolean
        # access check alone cannot establish continuity with the old snapshot.
        queued_natal.access.return_value = True
        release.set()
        await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    queued_natal.save.assert_not_awaited()
    queued_natal.publish.assert_not_awaited()
    queued_natal.bot.send_message.assert_not_awaited()
    queued_natal.bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_service_rejects_supplied_old_epoch_before_any_work(queued_natal):  # noqa: F811
    with pytest.raises(service.NatalAccessRevoked):
        await service.create_natal_report(queued_natal.birth, 777, 777, "https://bot.example.com", expected_epoch=99)
    queued_natal.resolve.assert_not_awaited()
    queued_natal.interpret.assert_not_awaited()
    queued_natal.save.assert_not_awaited()
    assert queued_natal.privacy.leases == {}
    assert queued_natal.privacy.captured_require_ltm == [False]


@pytest.mark.asyncio
async def test_first_natal_chat_initializes_only_missing_generation(queued_natal):  # noqa: F811
    queued_natal.privacy.has_chat = False
    assert await capture_natal_epoch(777) == 100
    queued_natal.privacy.ensure_generation.assert_awaited_once_with(777, expected_epoch=None)


@pytest.mark.asyncio
async def test_existing_legacy_zero_generation_is_exactly_revalidated(queued_natal):  # noqa: F811
    queued_natal.privacy.epoch = 0
    assert await capture_natal_epoch(777) == 0
    queued_natal.privacy.ensure_generation.assert_awaited_once_with(777, expected_epoch=0)


@pytest.mark.asyncio
async def test_delivery_lease_remains_owned_until_telegram_finishes(queued_natal):  # noqa: F811
    delivery_entered = asyncio.Event()
    finish_delivery = asyncio.Event()

    async def delayed_delivery(**kwargs):
        delivery_entered.set()
        await finish_delivery.wait()

    queued_natal.bot.send_message.side_effect = delayed_delivery
    task = asyncio.create_task(
        _build_and_send_natal_report(
            queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com", expected_epoch=100
        )
    )
    try:
        await asyncio.wait_for(delivery_entered.wait(), 1)
        assert list(queued_natal.privacy.leases.values()) == [(777, 100, "natal:delivery")]
        finish_delivery.set()
        await task
        assert queued_natal.privacy.leases == {}
        assert all(value is False for value in queued_natal.privacy.captured_require_ltm)
    finally:
        finish_delivery.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_http_ingress_generation_is_retained_while_queued(monkeypatch, queued_natal):  # noqa: F811
    queued = []
    monkeypatch.setattr("app.web_miniapp.submit_task", queued.append)
    token = make_valid_init_data(queued_natal.settings.TELEGRAM_BOT_TOKEN, user_id=777)
    response = await quart_app.test_client().post(
        "/webapp/api/natal/submit", headers={"Authorization": f"tma {token}"}, json={}
    )
    assert response.status_code == 200
    assert len(queued) == 1
    queued_natal.privacy.epoch = 200
    await queued[0]
    queued_natal.resolve.assert_not_awaited()
    queued_natal.interpret.assert_not_awaited()
    queued_natal.save.assert_not_awaited()
    queued_natal.publish.assert_not_awaited()
    queued_natal.bot.send_message.assert_not_awaited()
    assert queued_natal.privacy.leases == {}


@pytest.mark.asyncio
async def test_generation_changed_during_cover_lookup_prevents_delivery(monkeypatch, queued_natal):  # noqa: F811
    async def replaced_account_cover():
        queued_natal.privacy.epoch = 200
        return None

    monkeypatch.setattr("app.web_miniapp.get_natal_cover_photo", replaced_account_cover)
    await _build_and_send_natal_report(
        queued_natal.bot, 777, queued_natal.birth, "https://bot.example.com", expected_epoch=100
    )
    queued_natal.bot.send_message.assert_not_awaited()
    queued_natal.bot.send_photo.assert_not_awaited()

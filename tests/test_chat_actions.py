"""Progress indicators coalesce bursts and honor Telegram flood waits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from app.utils import chat_actions
from app.utils.background_tasks import TaskManager


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(chat_actions, "monotonic", lambda: clock.now)
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    return clock


async def test_concurrent_indicators_share_one_chat_budget(clock):
    async def send(**_kwargs):
        await asyncio.sleep(0)
        return True

    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=send))
    results = await asyncio.gather(
        *(chat_actions.send_chat_action(bot, 7, action) for action in ("typing", "upload_photo", "upload_voice"))
    )
    assert results == [True, False, False]
    bot.send_chat_action.assert_awaited_once_with(chat_id=7, action="typing")

    assert await chat_actions.send_chat_action(bot, 8, "upload_photo")
    clock.now += 4.0
    assert await chat_actions.send_chat_action(bot, 7, "upload_voice")
    assert bot.send_chat_action.await_count == 3


@pytest.mark.parametrize("timedelta_enabled", ["false", "true"])
async def test_flood_wait_skips_all_progress_until_deadline_without_replaying(clock, monkeypatch, timedelta_enabled):
    from telegram.warnings import PTBDeprecationWarning

    monkeypatch.setenv("PTB_TIMEDELTA", timedelta_enabled)
    bot = SimpleNamespace(send_chat_action=AsyncMock(return_value=True))

    async def limited_send():
        bot.send_chat_action.side_effect = RetryAfter(17)
        assert not await chat_actions.send_chat_action(bot, 7)

    if timedelta_enabled == "false":
        with pytest.warns(PTBDeprecationWarning):
            await limited_send()
    else:
        await limited_send()
    bot.send_chat_action.side_effect = None
    clock.now = 116.99
    assert not await chat_actions.send_chat_action(bot, 7, "upload_photo")
    assert not await chat_actions.send_chat_action(bot, 8, "upload_voice")
    bot.send_chat_action.assert_awaited_once()

    clock.now = 117.0
    assert await chat_actions.send_chat_action(bot, 8, "upload_voice")
    assert bot.send_chat_action.await_count == 2
    bot.send_chat_action.assert_awaited_with(chat_id=8, action="upload_voice")


async def test_shorter_concurrent_flood_wait_does_not_shorten_existing_pause(clock):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*, chat_id, **_kwargs):
        if chat_id == 7:
            entered.set()
            await release.wait()
            raise RetryAfter(5)
        raise RetryAfter(30)

    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=send))
    first = asyncio.create_task(chat_actions.send_chat_action(bot, 7))
    try:
        await entered.wait()
        assert not await chat_actions.send_chat_action(bot, 8)
    finally:
        release.set()
        await first
    bot.send_chat_action.side_effect = None
    bot.send_chat_action.return_value = True
    clock.now = 110.0
    assert not await chat_actions.send_chat_action(bot, 9)
    clock.now = 130.0
    assert await chat_actions.send_chat_action(bot, 9)


@pytest.mark.parametrize("error_type", [BadRequest, Forbidden, NetworkError, TimedOut])
async def test_expected_progress_failure_does_not_trigger_background_alert(clock, error_type):
    manager = TaskManager()
    alert = AsyncMock()
    manager.register_error_callback(alert)
    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=error_type("synthetic transport failure")))
    manager.submit(chat_actions.send_chat_action(bot, 7))
    assert await manager.drain()
    bot.send_chat_action.assert_awaited_once()
    alert.assert_not_awaited()


async def test_unexpected_failure_still_triggers_background_alert(clock):
    manager = TaskManager()
    alert = AsyncMock()
    manager.register_error_callback(alert)
    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=RuntimeError("synthetic programming error")))
    manager.submit(chat_actions.send_chat_action(bot, 7))
    assert await manager.drain()
    alert.assert_awaited_once()


async def test_slow_progress_times_out_and_joins_transport(clock, monkeypatch):
    cleaned = asyncio.Event()

    async def send(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(chat_actions, "_SEND_TIMEOUT_S", 0.01)
    bot = SimpleNamespace(send_chat_action=send)
    assert not await chat_actions.send_chat_action(bot, 7)
    assert cleaned.is_set()


async def test_cancellation_propagates_after_transport_cleanup_and_releases_admission(clock):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def send(**_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    bot = SimpleNamespace(send_chat_action=AsyncMock(side_effect=send))
    task = asyncio.create_task(chat_actions.send_chat_action(bot, 7))
    try:
        await entered.wait()
        clock.now += 10.0
        assert not await chat_actions.send_chat_action(bot, 7)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    bot.send_chat_action.side_effect = None
    bot.send_chat_action.return_value = True
    assert await chat_actions.send_chat_action(bot, 7)


async def test_capacity_sheds_cosmetic_work_and_reclaims_expired_chats(clock):
    bot = SimpleNamespace(send_chat_action=AsyncMock(return_value=True))
    for chat_id in range(1024):
        assert await chat_actions.send_chat_action(bot, chat_id)
    assert not await chat_actions.send_chat_action(bot, 1024)
    assert bot.send_chat_action.await_count == 1024

    clock.now += 4.0
    assert await chat_actions.send_chat_action(bot, 1024)
    assert bot.send_chat_action.await_count == 1025

"""Coalesced game refresh work has one tracked owner through every exit."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import crocodile
from app.games import crocodile_daily_telegram as daily
from app.games import crocodile_telegram as classic
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["daily", "classic"])
async def refresh(monkeypatch, request):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work(*_args, **_kwargs):
        calls.append(True)
        started.set()
        await release.wait()
        return []

    manager = TaskManager()
    bot = SimpleNamespace(username="testbot", edit_message_text=work)
    if request.param == "daily":
        module, tasks, pending = daily, daily._refresh_tasks, daily._pending_bots
        key = "2026-11-11"

        def queue():
            daily.queue_daily_result_refresh(bot, date(2026, 11, 11))

        reset = daily.reset_daily_telegram_state_for_tests
        monkeypatch.setattr(daily, "_REFRESH_DEBOUNCE_S", 0)
        monkeypatch.setattr(daily.repo, "get_active_result_messages", work)
    else:
        module, tasks, pending = classic, classic._thermometer_tasks, classic._pending_thermometer_updates
        key = "refresh-owner"
        game = SimpleNamespace(game_id="game", inline_message_id=key, best_score=0.7, status="active")
        monkeypatch.setattr(crocodile, "load_game", AsyncMock(return_value=game))

        def queue():
            classic.CrocodileTelegramService.queue_thermometer_update(bot, game)

        reset = classic.reset_telegram_state_for_tests
        monkeypatch.setattr(classic, "_THERMOMETER_DEBOUNCE_S", 0)
        monkeypatch.setattr(classic, "_build_play_keyboard", lambda *_args: None)
    reset()
    monkeypatch.setattr(module, "try_submit_task", manager.try_submit, raising=False)
    owners = []

    def enqueue():
        queue()
        owner = tasks.get(key)
        if owner is not None:
            owners.append(owner)

    try:
        yield SimpleNamespace(
            queue=enqueue,
            tasks=tasks,
            pending=pending,
            key=key,
            manager=manager,
            started=started,
            release=release,
            calls=calls,
        )
    finally:
        release.set()
        owners.extend(tasks.values())
        reset()
        await asyncio.gather(*owners, return_exceptions=True)
        await manager.drain(timeout=0, cancel_timeout=1)


async def test_refresh_is_owned_by_shared_shutdown_drain(refresh):
    refresh.queue()
    await asyncio.wait_for(refresh.started.wait(), 1)
    refresh.queue()
    assert refresh.key in refresh.pending
    assert await refresh.manager.drain(timeout=0, cancel_timeout=1)
    assert not refresh.tasks, "Game refresh escaped the shared shutdown drain"
    assert not refresh.pending
    assert len(refresh.calls) == 1


async def test_cancelled_refresh_does_not_restart_pending_work(refresh):
    refresh.queue()
    await asyncio.wait_for(refresh.started.wait(), 1)
    owner = refresh.tasks[refresh.key]
    refresh.queue()
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    assert not refresh.tasks, "Cancellation restarted a coalesced game refresh"
    assert not refresh.pending


async def test_unstarted_refresh_releases_its_exact_registry_entry(refresh):
    refresh.queue()
    owner = refresh.tasks[refresh.key]
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    assert not refresh.tasks
    assert not refresh.pending


async def test_rejected_refresh_leaves_no_hidden_pending_work(refresh):
    refresh.manager.MAX_TASKS = 0
    refresh.queue()
    assert not refresh.tasks
    assert not refresh.pending
    assert not refresh.calls


async def test_terminal_game_cannot_be_overwritten_by_a_stale_thermometer(monkeypatch):
    classic.reset_telegram_state_for_tests()
    manager = TaskManager()
    monkeypatch.setattr(classic, "try_submit_task", manager.try_submit)
    monkeypatch.setattr(classic, "_THERMOMETER_DEBOUNCE_S", 0)
    game = SimpleNamespace(game_id="terminal", inline_message_id="terminal-inline", best_score=0.8, status="won")
    monkeypatch.setattr(crocodile, "load_game", AsyncMock(return_value=game))
    bot = SimpleNamespace(edit_message_text=AsyncMock())
    classic.CrocodileTelegramService.queue_thermometer_update(bot, game)
    assert await manager.drain(timeout=1)
    bot.edit_message_text.assert_not_awaited()


async def test_finalization_drains_inflight_thermometer_before_terminal_edit(monkeypatch):
    classic.reset_telegram_state_for_tests()
    manager = TaskManager()
    monkeypatch.setattr(classic, "try_submit_task", manager.try_submit)
    monkeypatch.setattr(classic, "_THERMOMETER_DEBOUNCE_S", 0)
    monkeypatch.setattr(classic, "_build_play_keyboard", lambda *_args: None)
    started, release = asyncio.Event(), asyncio.Event()
    visible = []

    async def edit(**kwargs):
        if "идёт игра" in kwargs["text"]:
            started.set()
            await release.wait()
        visible.append(kwargs["text"])

    game = SimpleNamespace(
        game_id="terminal",
        inline_message_id="terminal-inline",
        best_score=0.8,
        status="active",
        target_word="cat",
        attempts=["cat"],
        max_attempts=6,
    )
    monkeypatch.setattr(crocodile, "load_game", AsyncMock(return_value=game))
    bot = SimpleNamespace(edit_message_text=edit)
    classic.CrocodileTelegramService.queue_thermometer_update(bot, game)
    try:
        await asyncio.wait_for(started.wait(), 1)
        game.status = "won"
        await classic.CrocodileTelegramService.finalize_game(bot, game)
        release.set()
        assert await manager.drain(timeout=1)
        assert len(visible) == 1
        assert "Угадано" in visible[-1]
    finally:
        release.set()
        owners = list(classic._thermometer_tasks.values())
        classic.reset_telegram_state_for_tests()
        await asyncio.gather(*owners, return_exceptions=True)
        await manager.drain(timeout=0, cancel_timeout=1)

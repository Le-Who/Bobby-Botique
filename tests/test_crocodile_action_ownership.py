"""A retry of one admitted guess must not consume another attempt."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from quart.wrappers import Websocket

from app.games import crocodile, crocodile_daily, crocodile_runtime
from app.repos import crocodile_daily as repo
from app.web import quart_app
from tests.factories import make_valid_init_data


@pytest.fixture
def croc_store(monkeypatch, request):
    mode = getattr(request, "param", "daily")
    token = make_valid_init_data("test-token", user_id=777)
    store = SimpleNamespace(mode=mode)
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "update_user_display_name", AsyncMock())
    monkeypatch.setattr(repo, "record_player_activity", AsyncMock())
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: None)
    monkeypatch.setattr(crocodile_runtime, "redis_client", None)
    monkeypatch.setattr(
        "app.games.judge.judge_guess",
        AsyncMock(return_value=("wrong", SimpleNamespace(score=0.0, hint="Cold", cached=False))),
    )
    crocodile_runtime.reset_runtime_state_for_tests()
    if mode == "daily":
        puzzle = repo.DailyPuzzle(date(2026, 10, 25), "crocodile", "animals", "en")
        store.result = repo.DailyResult(777, puzzle.puzzle_date, "active", [], 0.0, 0, None, None, 0, "", 0)

        async def get_state(user_id, **kwargs):
            assert user_id == 777
            return puzzle, store.result

        async def overview(user_id):
            assert user_id == 777
            return puzzle.puzzle_date, {"easy": puzzle}, {"easy": store.result}

        async def append_attempt(**values):
            assert values["user_id"] == 777 and values["difficulty"] == "easy"
            store.result = replace(store.result, attempts=[*store.result.attempts, values["attempt"]])
            return store.result

        monkeypatch.setattr(crocodile_daily, "get_daily_state", get_state)
        monkeypatch.setattr(crocodile_daily, "get_daily_overview", overview)
        monkeypatch.setattr(repo, "append_attempt_and_maybe_finish", append_attempt)
        store.attempts = lambda: store.result.attempts
        store.path = "/webapp/game/daily/ws?initData=" + quote(token)
    else:
        game = crocodile.CrocodileGame("action-owner", "crocodile", "animals", "en", "inline-test", 999, 777)
        store.game = game
        monkeypatch.setattr(crocodile, "load_game", AsyncMock(return_value=game))
        monkeypatch.setattr(crocodile.CrocodileGameRepository, "save", AsyncMock(return_value=True))
        store.attempts = lambda: game.attempts
        store.path = "/webapp/game/ws?game_id=action-owner&initData=" + quote(token)
    try:
        yield store
    finally:
        crocodile_runtime.reset_runtime_state_for_tests()
        crocodile._mem_history.pop("action-owner", None)


@pytest.mark.asyncio
async def test_daily_concurrent_retry_counts_one_wrong_guess(monkeypatch, croc_store):
    original_lookup = crocodile_runtime.get_cached_pending_action_result
    both_missed = asyncio.Event()
    misses = 0

    async def simultaneous_lookup(game_id, pending_id):
        nonlocal misses
        cached = await original_lookup(game_id, pending_id)
        if misses < 2:
            misses += 1
            if misses == 2:
                both_missed.set()
            await both_missed.wait()
        return cached

    monkeypatch.setattr(crocodile_runtime, "get_cached_pending_action_result", simultaneous_lookup)
    payload = json.dumps({"type": "guess", "word": "stone", "pending_id": "same-guess"})
    async with (
        quart_app.test_client().websocket(croc_store.path) as first,
        quart_app.test_client().websocket(croc_store.path) as second,
    ):
        await first.receive()
        await second.receive()
        await asyncio.gather(first.send(payload), second.send(payload))
        responses = await asyncio.gather(first.receive(), second.receive())
    assert len(croc_store.attempts()) == 1
    first_event, second_event = map(json.loads, responses)
    assert first_event == second_event
    assert first_event["attempts"] == 1
    assert first_event["status"] == "wrong"
    assert first_event["score"] == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("croc_store", ["daily", "classic"], indirect=True)
async def test_cancelled_guess_finishes_replay_before_releasing_lock(monkeypatch, croc_store):
    stamp_started = asyncio.Event()
    finish_stamp = asyncio.Event()
    retry_waiting = asyncio.Event()
    owners = []
    original_lock = crocodile_runtime.game_mutation_lock
    original_stamp = crocodile_runtime.stamp_runtime_payload

    @asynccontextmanager
    async def observed_lock(game_id):
        owners.append(asyncio.current_task())
        if len(owners) == 2:
            retry_waiting.set()
        async with original_lock(game_id):
            yield

    async def suspended_stamp(game_id, payload):
        if payload.get("event") == "result" and not stamp_started.is_set():
            stamp_started.set()
            await finish_stamp.wait()
        return await original_stamp(game_id, payload)

    monkeypatch.setattr(crocodile_runtime, "game_mutation_lock", observed_lock)
    monkeypatch.setattr(crocodile_runtime, "stamp_runtime_payload", suspended_stamp)
    payload = json.dumps({"type": "guess", "word": "stone", "pending_id": "cancelled-guess"})
    try:
        async with (
            quart_app.test_client().websocket(croc_store.path) as first,
            quart_app.test_client().websocket(croc_store.path) as second,
        ):
            await first.receive()
            await second.receive()
            await first.send(payload)
            await asyncio.wait_for(stamp_started.wait(), 1)
            assert len(croc_store.attempts()) == 1
            owners[0].cancel()
            await second.send(payload)
            await asyncio.wait_for(retry_waiting.wait(), 1)
            owners[0].cancel()
            finish_stamp.set()
            broadcasts = []
            while True:
                replay = json.loads(await asyncio.wait_for(second.receive(), 1))
                if replay.get("event") != "spectator_result":
                    break
                broadcasts.append(replay)
        assert len(croc_store.attempts()) == 1
        assert replay["attempts"] == 1
        assert replay["status"] == "wrong"
        assert replay["score"] == 0.0
        if croc_store.mode == "classic":
            assert len(broadcasts) == 1
            assert broadcasts[0]["word"] == "stone"
            assert broadcasts[0]["status"] == "wrong"
    finally:
        finish_stamp.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("croc_store", ["classic"], indirect=True)
async def test_cancelled_winning_guess_finishes_required_publication(monkeypatch, croc_store):
    stamp_started = asyncio.Event()
    finish_stamp = asyncio.Event()
    owner = None
    finalized = []
    original_lock = crocodile_runtime.game_mutation_lock
    original_stamp = crocodile_runtime.stamp_runtime_payload

    @asynccontextmanager
    async def observed_lock(game_id):
        nonlocal owner
        owner = asyncio.current_task()
        async with original_lock(game_id):
            yield

    async def suspended_stamp(game_id, payload):
        if payload.get("event") == "result" and not stamp_started.is_set():
            stamp_started.set()
            await finish_stamp.wait()
        return await original_stamp(game_id, payload)

    async def finalize(bot):
        assert bot is sentinel_bot
        finalized.append(croc_store.game.status)
        # The real finalizer removes game runtime state, including pending
        # replies; accepted terminal replay must survive that cleanup.
        await crocodile_runtime.clear_runtime_state(croc_store.game.game_id)

    sentinel_bot = object()
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: sentinel_bot)
    monkeypatch.setattr(crocodile_runtime, "game_mutation_lock", observed_lock)
    monkeypatch.setattr(crocodile_runtime, "stamp_runtime_payload", suspended_stamp)
    monkeypatch.setattr(croc_store.game, "finalize", finalize)
    monkeypatch.setattr(
        "app.games.judge.judge_guess",
        AsyncMock(return_value=("exact_match", SimpleNamespace(score=1.0, hint="Exact", cached=False))),
    )
    try:
        async with quart_app.test_client().websocket(croc_store.path) as socket:
            await socket.receive()
            await socket.send(json.dumps({"type": "guess", "word": "crocodile", "pending_id": "win"}))
            await asyncio.wait_for(stamp_started.wait(), 1)
            owner.cancel()
            finish_stamp.set()
        assert finalized == ["won"]
        cached = await crocodile_runtime.get_cached_pending_action_result("action-owner", "win")
        assert cached is not None and cached["status"] == "exact_match"
        assert cached["attempts"] == 1
    finally:
        finish_stamp.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("croc_store", ["classic"], indirect=True)
@pytest.mark.parametrize(
    "send_fails", [False, True, "cancel"], ids=["prompt-result", "failed-send-still-finalizes", "cancelled-send"]
)
async def test_winning_reply_precedes_slow_finalization(monkeypatch, croc_store, send_fails):
    finalize_entered = asyncio.Event()
    finish_finalize = asyncio.Event()
    finalized = []
    original_send = Websocket.send_json

    async def send_json(socket, payload, **kwargs):
        if send_fails and payload.get("event") == "result":
            if send_fails == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("synthetic closed socket")
        await original_send(socket, payload, **kwargs)

    async def finalize(bot):
        finalize_entered.set()
        await finish_finalize.wait()
        finalized.append(croc_store.game.status)

    monkeypatch.setattr(Websocket, "send_json", send_json)
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: object())
    monkeypatch.setattr(croc_store.game, "finalize", finalize)
    monkeypatch.setattr(
        "app.games.judge.judge_guess",
        AsyncMock(return_value=("exact_match", SimpleNamespace(score=1.0, hint="Exact", cached=False))),
    )
    async with quart_app.test_client().websocket(croc_store.path) as socket:
        try:
            await socket.receive()
            await socket.send(json.dumps({"type": "guess", "word": "crocodile", "pending_id": "prompt-win"}))
            await asyncio.wait_for(finalize_entered.wait(), 1)
            if not send_fails:
                response = json.loads(await asyncio.wait_for(socket.receive(), 1))
                assert response["status"] == "exact_match"
                assert response["attempts"] == 1
                assert response["score"] == 1.0
                assert not finalized
        finally:
            finish_finalize.set()
    assert finalized == ["won"]

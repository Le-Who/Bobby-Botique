"""Concurrent retries of one move must reuse its committed response."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from app.games import crocodile_runtime
from app.repos import daily_2048 as repo
from app.web import quart_app
from tests.factories import make_valid_init_data


@pytest.fixture
def move_store(monkeypatch):
    # Keep the reducer, pending-result cache and mutation lock real; replace DB
    # transport and Telegram authorization only.
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 10, 25),
        board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 0, "y": 3, "value": 2}, {"x": 2, "y": 3, "value": 2}],
        seed="action-ownership",
    )
    stored = repo.Daily2048Result(
        user_id=777,
        puzzle_date=puzzle.puzzle_date,
        status="active",
        board=puzzle.board,
        spawn_index=0,
        moves=0,
        merge_score=0,
        final_score=0,
        elapsed_ms=0,
        started_at=datetime(2026, 10, 25, tzinfo=UTC),
        won_at=None,
        finished_at=None,
    )

    async def load_result(user_id, current_puzzle):
        assert user_id == 777 and current_puzzle == puzzle
        return stored

    async def persist(**values):
        nonlocal stored
        stored = replace(
            stored,
            **{
                key: values[key]
                for key in ("board", "spawn_index", "moves", "merge_score", "elapsed_ms", "status", "final_score")
            },
            recordable=stored.recordable and values["recordable"],
        )
        return stored

    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.update_user_display_name", AsyncMock())
    monkeypatch.setattr(repo, "ensure_today_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(repo, "get_or_create_result", load_result)
    monkeypatch.setattr(repo, "update_result_after_move", persist)
    monkeypatch.setattr(crocodile_runtime, "redis_client", None)
    crocodile_runtime.reset_runtime_state_for_tests()
    path = "/webapp/daily2048/ws?initData=" + quote(make_valid_init_data("test-token", user_id=777))
    try:
        yield SimpleNamespace(puzzle=puzzle, read=lambda: stored, path=path)
    finally:
        crocodile_runtime.reset_runtime_state_for_tests()


@pytest.mark.asyncio
async def test_concurrent_websocket_retry_applies_one_move(monkeypatch, move_store):

    # Force both connections to observe the initial cache miss before either
    # gets the mutation lock. Later in-lock rechecks must read the actual cache.
    original_lookup = crocodile_runtime.get_cached_pending_action_result
    first_lookups = 0
    both_missed = asyncio.Event()

    async def concurrent_lookup(game_id, pending_id):
        nonlocal first_lookups
        cached = await original_lookup(game_id, pending_id)
        if first_lookups < 2:
            first_lookups += 1
            if first_lookups == 2:
                both_missed.set()
            await both_missed.wait()
        return cached

    monkeypatch.setattr(crocodile_runtime, "get_cached_pending_action_result", concurrent_lookup)
    path = move_store.path
    payload = json.dumps({"type": "move", "direction": "right", "pending_id": "same-move", "client_elapsed_ms": 50})
    try:
        async with (
            quart_app.test_client().websocket(path) as first,
            quart_app.test_client().websocket(path) as second,
        ):
            await first.receive()
            await second.receive()
            await asyncio.gather(first.send(payload), second.send(payload))
            responses = await asyncio.gather(first.receive(), second.receive())
        assert move_store.read().moves == 1
        first_event, second_event = map(json.loads, responses)
        assert first_event == second_event
        assert first_event["moves"] == 1
        assert first_event["board"] == [[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [2, 0, 0, 0]]
    finally:
        crocodile_runtime.reset_runtime_state_for_tests()


@pytest.mark.asyncio
async def test_cancelled_socket_finishes_committed_move_cache_before_retry(monkeypatch, move_store):
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
        if payload.get("event") == "move_result" and not stamp_started.is_set():
            stamp_started.set()
            await finish_stamp.wait()
        return await original_stamp(game_id, payload)

    monkeypatch.setattr(crocodile_runtime, "game_mutation_lock", observed_lock)
    monkeypatch.setattr(crocodile_runtime, "stamp_runtime_payload", suspended_stamp)
    payload = json.dumps({"type": "move", "direction": "right", "pending_id": "cancelled-move"})
    try:
        async with (
            quart_app.test_client().websocket(move_store.path) as first,
            quart_app.test_client().websocket(move_store.path) as second,
        ):
            await first.receive()
            await second.receive()
            await first.send(payload)
            await asyncio.wait_for(stamp_started.wait(), 1)
            assert move_store.read().moves == 1
            owners[0].cancel()
            await second.send(payload)
            await asyncio.wait_for(retry_waiting.wait(), 1)
            # Repeated transport cancellation must still retain the mutex until
            # the accepted operation makes its replayable response available.
            owners[0].cancel()
            finish_stamp.set()
            replay = json.loads(await asyncio.wait_for(second.receive(), 1))
        assert move_store.read().moves == 1
        assert replay["moves"] == 1
        assert replay["board"] == [[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [2, 0, 0, 0]]
    finally:
        finish_stamp.set()

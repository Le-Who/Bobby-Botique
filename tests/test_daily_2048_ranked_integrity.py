"""Ranked provenance survives optimistic recovery without disabling gameplay."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from app.games import daily_2048, daily_2048_telegram
from app.repos import daily_2048 as repo
from app.web import quart_app
from tests.factories import make_valid_init_data


@pytest.fixture
def game_store(monkeypatch):
    """Replace DB transport only; the actual move reducer and payload run."""
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 10, 9),
        board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}, {"x": 1, "y": 3, "value": 2}],
        seed="ranked-provenance",
        par_moves=12,
        target_seconds=180,
    )
    store = SimpleNamespace(
        puzzle=puzzle,
        result=repo.Daily2048Result(
            user_id=777,
            puzzle_date=puzzle.puzzle_date,
            status="active",
            board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            spawn_index=0,
            moves=0,
            merge_score=0,
            final_score=0,
            elapsed_ms=0,
            started_at=datetime(2026, 10, 9, 8, 0, tzinfo=UTC),
            won_at=None,
            finished_at=None,
        ),
    )

    async def load_result(user_id, puzzle):
        assert user_id == store.result.user_id and puzzle.puzzle_date == store.result.puzzle_date
        return store.result

    async def persist(**kwargs):
        # Persistence itself is separately checked against real PostgreSQL.
        store.result = replace(
            store.result,
            **{
                key: kwargs[key]
                for key in ("board", "spawn_index", "moves", "merge_score", "elapsed_ms", "status", "final_score")
            },
            recordable=store.result.recordable and kwargs.get("recordable", True),
        )
        return store.result

    monkeypatch.setattr(repo, "ensure_today_puzzle", AsyncMock(return_value=puzzle))
    monkeypatch.setattr(repo, "get_or_create_result", load_result)
    monkeypatch.setattr(repo, "update_result_after_move", persist)
    return store


@pytest.mark.asyncio
async def test_fabricated_winning_pair_completes_game_without_ranked_provenance(game_store):
    # Reverting provenance propagation permits this unreachable board to rank.
    event = await daily_2048.process_move(
        777,
        "left",
        now=datetime(2026, 10, 9, 8, 10, tzinfo=UTC),
        client_elapsed_ms=0,
        client_board_before=[[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        client_board_after=[[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
    )

    assert event["board"] == [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    assert event["status"] == "won"
    assert event["daily2048_completed"] is True
    assert event["game_over"] is True
    assert event["moves"] == 1
    assert event["merge_score"] == 256
    assert event["elapsed_ms"] == 0
    assert event["final_score"] == 4880
    assert event["recordable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recordable", [True, False], ids=["ranked", "already-unranked"])
@pytest.mark.parametrize("client_elapsed_ms", [0, 25_000])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (None, None),
        (
            [[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            [[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        ),
        ("malformed", "malformed"),
        ([[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], None),
        (None, [[0, 0, 0, 256], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]),
        (
            [[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            [[0, 0, 0, 512], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        ),
    ],
    ids=["server-only", "matching-pair", "malformed", "before-only", "after-only", "inconsistent-pair"],
)
async def test_trusted_or_rejected_client_pair_preserves_recordability(
    game_store, before, after, client_elapsed_ms, recordable
):
    game_store.result.recordable = recordable
    event = await daily_2048.process_move(
        777,
        "right",
        now=datetime(2026, 10, 9, 8, 10, tzinfo=UTC),
        client_elapsed_ms=client_elapsed_ms,
        client_board_before=before,
        client_board_after=after,
    )

    assert event["board"] == [[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    assert event["status"] == "active"
    assert event["recordable"] is recordable
    assert event["moves"] == 1
    assert event["merge_score"] == 0
    assert event["elapsed_ms"] == client_elapsed_ms
    assert event["final_score"] == 0


@pytest.mark.asyncio
async def test_lag_recovery_and_later_server_move_stay_unranked(game_store):
    recovered = await daily_2048.process_move(
        777,
        "left",
        now=datetime(2026, 10, 9, 8, 10, tzinfo=UTC),
        client_elapsed_ms=25_000,
        client_board_before=[[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        client_board_after=[[4, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
    )
    assert recovered["board"] == [[4, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    assert recovered["merge_score"] == 4
    assert recovered["recordable"] is False

    later = await daily_2048.process_move(
        777,
        "up",
        now=datetime(2026, 10, 9, 8, 11, tzinfo=UTC),
        client_elapsed_ms=26_000,
    )
    assert later["board"] == [[4, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 2, 0, 0]]
    assert later["moves"] == 2
    assert later["merge_score"] == 4
    assert later["elapsed_ms"] == 26_000
    assert later["recordable"] is False


@pytest.fixture
def websocket_identity(monkeypatch):
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.repos.users.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.crocodile_daily.update_user_display_name", AsyncMock())
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: None)
    monkeypatch.setattr(
        daily_2048_telegram,
        "render_completion_event",
        AsyncMock(
            return_value={
                "rank": None,
                "leaderboard": [],
                "puzzle_date": "2026-10-09",
            }
        ),
    )
    return "/webapp/daily2048/ws?initData=" + quote(make_valid_init_data("test-token", user_id=777))


@pytest.mark.asyncio
async def test_authenticated_websocket_exploit_remains_unranked_on_reconnect(game_store, websocket_identity):
    async with quart_app.test_client().websocket(websocket_identity) as ws:
        initial = json.loads(await ws.receive())
        assert initial["recordable"] is True
        await ws.send(
            json.dumps(
                {
                    "type": "move",
                    "direction": "left",
                    "pending_id": "ranked-integrity-win",
                    "client_elapsed_ms": 0,
                    "client_board_before": [[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                    "client_board_after": [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                }
            )
        )
        won = json.loads(await ws.receive())
        assert won["daily2048_completed"] is True
        assert won["recordable"] is False
        assert won["rank"] is None
        assert won["board"] == [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]

    async with quart_app.test_client().websocket(websocket_identity) as ws:
        resumed = json.loads(await ws.receive())
        completion = json.loads(await ws.receive())
        assert resumed["status"] == "won"
        assert resumed["recordable"] is False
        assert resumed["can_practice"] is True
        assert completion["event"] == "daily2048_completed"
        assert completion["final_score"] == 4880
        assert completion["recordable"] is False
        assert completion["rank"] is None
        await ws.send(json.dumps({"type": "move", "direction": "right"}))
        practice = json.loads(await ws.receive())
        assert practice["recordable"] is False
        assert practice["daily2048_completed"] is False
        assert practice["status"] == "active"


@pytest.mark.asyncio
async def test_unranked_won_reconnect_keeps_completion_unranked(game_store, websocket_identity):
    game_store.result = replace(game_store.result, status="won", final_score=4880, recordable=False)
    async with quart_app.test_client().websocket(websocket_identity) as ws:
        resumed = json.loads(await ws.receive())
        completion = json.loads(await ws.receive())
        assert resumed["recordable"] is False
        assert completion["event"] == "daily2048_completed"
        assert completion["final_score"] == 4880
        assert completion["recordable"] is False
        assert completion["rank"] is None


@pytest.mark.asyncio
async def test_unranked_active_reconnect_does_not_restore_recording(game_store, websocket_identity):
    game_store.result.recordable = False
    async with quart_app.test_client().websocket(websocket_identity) as ws:
        resumed = json.loads(await ws.receive())
        assert resumed["status"] == "active"
        assert resumed["recordable"] is False
        await ws.send(json.dumps({"type": "move", "direction": "right", "client_elapsed_ms": 25_000}))
        moved = json.loads(await ws.receive())
        assert moved["board"] == [[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
        assert moved["recordable"] is False


@pytest.mark.asyncio
async def test_unranked_completion_message_shows_result_without_claiming_daily_record(game_store, monkeypatch):
    game_store.result = replace(game_store.result, status="won", final_score=4880, moves=1, recordable=False)
    monkeypatch.setattr(repo, "get_result", AsyncMock(return_value=game_store.result))
    monkeypatch.setattr(repo, "get_rank", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "get_leaderboard", AsyncMock(return_value=[]))

    text, keyboard = await daily_2048_telegram.render_result_body(777, game_store.puzzle.puzzle_date)

    assert "4880" in text
    assert "не участвует в рейтинге" in text
    assert "рекорд дня уже зафиксирован" not in text
    assert "Место дня" not in text
    assert any(button.text == "Открыть 2048" for row in keyboard.inline_keyboard for button in row)

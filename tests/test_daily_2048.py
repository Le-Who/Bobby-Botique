from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.games import daily_2048, daily_2048_telegram
from app.handlers import cmd_admin
from app.handlers import daily_2048 as daily_2048_handler
from app.repos import daily_2048 as repo
from app.web import quart_app
from tests.factories import make_valid_init_data

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "app" / "templates" / "daily_2048.html"


def test_slide_merges_each_tile_once_and_reports_score() -> None:
    outcome = daily_2048.apply_move(
        [
            [2, 2, 2, 0],
            [4, 4, 4, 4],
            [0, 0, 8, 8],
            [16, 0, 16, 16],
        ],
        "left",
    )

    assert outcome.moved is True
    assert outcome.board == [
        [4, 2, 0, 0],
        [8, 8, 0, 0],
        [16, 0, 0, 0],
        [32, 16, 0, 0],
    ]
    assert outcome.gained_score == 4 + 8 + 8 + 16 + 32


def test_spawn_uses_daily_sequence_with_stable_fallback() -> None:
    board = [
        [4, 2, 0, 0],
        [8, 8, 0, 0],
        [16, 0, 0, 0],
        [32, 16, 0, 0],
    ]
    spawned = daily_2048.spawn_tile(
        board,
        [{"x": 2, "y": 0, "value": 4}],
        spawn_index=0,
        seed="2026-06-02",
    )

    assert spawned.board[0][2] == 4
    assert spawned.spawn_index == 1

    fallback = daily_2048.spawn_tile(
        board,
        [{"x": 0, "y": 0, "value": 4}],
        spawn_index=0,
        seed="2026-06-02",
    )

    assert fallback.board != board
    assert sum(1 for row in fallback.board for value in row if value) == 8


def test_goal_and_score_make_speed_and_moves_visible() -> None:
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 6, 2),
        board=[[0, 0, 0, 0], [0, 0, 0, 0], [2, 4, 8, 16], [32, 64, 128, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[],
        seed="2026-06-02",
        par_moves=64,
        target_seconds=240,
        status="ready",
    )

    assert daily_2048.goal_reached([[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], puzzle)
    assert not daily_2048.goal_reached([[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], puzzle)

    fast = daily_2048.compute_final_score(puzzle, moves=42, elapsed_ms=125_000, merge_score=780)
    slow = daily_2048.compute_final_score(puzzle, moves=54, elapsed_ms=230_000, merge_score=780)

    assert fast > slow


def test_default_puzzle_generation_avoids_used_challenge_signatures() -> None:
    puzzle_date = date(2026, 6, 9)
    first = repo.build_default_puzzle(puzzle_date)
    used = {repo.puzzle_signature(first)}

    regenerated = repo.build_default_puzzle(puzzle_date, used_signatures=used)

    assert repo.puzzle_signature(regenerated) not in used
    assert repo.board_signature(regenerated.board) != repo.board_signature(first.board)
    assert regenerated.status == "ready"


@pytest.mark.asyncio
async def test_ensure_puzzle_generates_non_repeating_challenge_before_upsert(monkeypatch) -> None:
    puzzle_date = date(2026, 6, 10)
    first_candidate = repo.build_default_puzzle(puzzle_date)
    used_signature = repo.puzzle_signature(first_candidate)
    captured: dict[str, object] = {}

    async def fake_upsert(puzzle_date_arg, **kwargs):
        captured["date"] = puzzle_date_arg
        captured.update(kwargs)
        return repo.Daily2048Puzzle(puzzle_date=puzzle_date_arg, prepared_at=None, **kwargs)

    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "get_existing_puzzle_signatures", AsyncMock(return_value={used_signature}))
    monkeypatch.setattr(repo, "get_existing_board_signatures", AsyncMock(return_value=set()))
    monkeypatch.setattr(repo, "upsert_puzzle", fake_upsert)

    puzzle = await repo.ensure_puzzle(puzzle_date)

    assert repo.puzzle_signature(puzzle) != used_signature
    assert repo.board_signature(captured["board"]) != repo.board_signature(first_candidate.board)
    assert captured["status"] == "ready"


@pytest.mark.asyncio
async def test_ensure_puzzle_avoids_reusing_existing_starting_board(monkeypatch) -> None:
    puzzle_date = date(2026, 6, 11)
    first_candidate = repo.build_default_puzzle(puzzle_date)
    used_board = repo.board_signature(first_candidate.board)
    captured: dict[str, object] = {}

    async def fake_upsert(puzzle_date_arg, **kwargs):
        captured["date"] = puzzle_date_arg
        captured.update(kwargs)
        return repo.Daily2048Puzzle(puzzle_date=puzzle_date_arg, prepared_at=None, **kwargs)

    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "get_existing_puzzle_signatures", AsyncMock(return_value=set()))
    monkeypatch.setattr(repo, "get_existing_board_signatures", AsyncMock(return_value={used_board}))
    monkeypatch.setattr(repo, "upsert_puzzle", fake_upsert)

    puzzle = await repo.ensure_puzzle(puzzle_date)

    assert repo.board_signature(puzzle.board) != used_board
    assert repo.board_signature(captured["board"]) != used_board


@pytest.mark.asyncio
async def test_repo_upsert_custom_puzzle_persists_explicit_board_and_goal(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_db_query(query, params=(), retries=3, conn=None):
        captured["query"] = query
        captured["params"] = params
        return [
            {
                "puzzle_date": date(2026, 6, 2),
                "board": params[1],
                "goal_type": params[2],
                "goal_value": params[3],
                "spawn_sequence": params[4],
                "seed": params[5],
                "par_moves": params[6],
                "target_seconds": params[7],
                "status": params[8],
                "solution_moves": params[9],
                "prepared_at": datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", fake_db_query)

    puzzle = await repo.upsert_puzzle(
        date(2026, 6, 2),
        board=[[0, 0, 0, 0], [0, 0, 0, 0], [2, 4, 8, 16], [32, 64, 128, 0]],
        goal_type="tile",
        goal_value=512,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="custom",
        par_moves=72,
        target_seconds=240,
        status="ready",
        solution_moves="left,down,right",
    )

    assert "daily_2048_puzzles" in str(captured["query"])
    assert puzzle.goal_value == 512
    assert puzzle.spawn_sequence == [{"x": 3, "y": 3, "value": 2}]


@pytest.mark.asyncio
async def test_process_move_records_first_solution_then_keeps_practice_unranked() -> None:
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 6, 2),
        board=[[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="win-fast",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    active = repo.Daily2048Result(
        user_id=77,
        puzzle_date=date(2026, 6, 2),
        status="active",
        board=puzzle.board,
        spawn_index=0,
        moves=0,
        merge_score=0,
        final_score=0,
        elapsed_ms=0,
        started_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
        won_at=None,
        finished_at=None,
        recordable=True,
    )
    won = repo.Daily2048Result(
        **{
            **active.__dict__,
            "status": "won",
            "board": [[256, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            "spawn_index": 1,
            "moves": 1,
            "merge_score": 256,
            "final_score": 5940,
            "elapsed_ms": 30_000,
            "won_at": datetime(2026, 6, 2, 8, 0, 30, tzinfo=UTC),
            "finished_at": datetime(2026, 6, 2, 8, 0, 30, tzinfo=UTC),
        }
    )

    with (
        patch("app.games.daily_2048.repo.ensure_today_puzzle", new_callable=AsyncMock, return_value=puzzle),
        patch("app.games.daily_2048.repo.get_or_create_result", new_callable=AsyncMock, return_value=active),
        patch("app.games.daily_2048.repo.update_result_after_move", new_callable=AsyncMock, return_value=won) as update_mock,
    ):
        event = await daily_2048.process_move(77, "left", now=datetime(2026, 6, 2, 8, 0, 30, tzinfo=UTC))

    update_mock.assert_awaited_once()
    assert event["daily2048_completed"] is True
    assert event["recordable"] is True
    assert event["moves"] == 1
    assert event["final_score"] == 5940

    practice = await daily_2048.process_practice_move(
        won,
        puzzle,
        "left",
        now=datetime(2026, 6, 2, 8, 1, 0, tzinfo=UTC),
    )
    assert practice["recordable"] is False
    assert practice["daily2048_completed"] is False
    assert practice["final_score"] == 5940


@pytest.mark.asyncio
async def test_result_message_contains_moves_score_and_monthly_champions_button() -> None:
    puzzle_date = date(2026, 6, 2)
    result = repo.Daily2048Result(
        user_id=77,
        puzzle_date=puzzle_date,
        status="won",
        board=[[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        spawn_index=10,
        moves=42,
        merge_score=820,
        final_score=6120,
        elapsed_ms=145_000,
        started_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
        won_at=datetime(2026, 6, 2, 8, 2, 25, tzinfo=UTC),
        finished_at=datetime(2026, 6, 2, 8, 2, 25, tzinfo=UTC),
        recordable=True,
    )

    with (
        patch("app.games.daily_2048_telegram.repo.get_result", new_callable=AsyncMock, return_value=result),
        patch("app.games.daily_2048_telegram.repo.get_rank", new_callable=AsyncMock, return_value=3),
        patch(
            "app.games.daily_2048_telegram.repo.get_leaderboard",
            new_callable=AsyncMock,
            return_value=[{"user_id": 77, "display_name": "Mira", "final_score": 6120, "moves": 42}],
        ),
    ):
        text, keyboard = await daily_2048_telegram.render_result_body(77, puzzle_date)

    assert "42" in text
    assert "6120" in text
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Лучшие за месяц" in labels


@pytest.mark.asyncio
async def test_daily2048_websocket_sends_explicit_goal_and_accepts_move(monkeypatch) -> None:
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    init_data = make_valid_init_data("test-token", user_id=777)
    url = f"/webapp/daily2048/ws?initData={urllib.parse.quote(init_data)}&tz=Europe%2FKyiv"
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 6, 2),
        board=[[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[],
        seed="ws",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    result = repo.Daily2048Result(
        user_id=777,
        puzzle_date=puzzle.puzzle_date,
        status="active",
        board=puzzle.board,
        spawn_index=0,
        moves=0,
        merge_score=0,
        final_score=0,
        elapsed_ms=0,
        started_at=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
        won_at=None,
        finished_at=None,
        recordable=True,
    )

    with (
        patch("app.games.daily_2048.get_daily_state", new_callable=AsyncMock, return_value=(puzzle, result)),
        patch("app.games.daily_2048.process_move", new_callable=AsyncMock) as move_mock,
        patch("app.repos.crocodile_daily.update_timezone_if_known", new_callable=AsyncMock),
        patch("app.repos.crocodile_daily.update_user_display_name", new_callable=AsyncMock),
        patch("app.bot_instance.get_bot", return_value=None),
    ):
        move_mock.return_value = {
            "event": "move_result",
            "board": [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            "moves": 1,
            "merge_score": 256,
            "final_score": 5900,
            "recordable": True,
            "daily2048_completed": True,
        }
        async with quart_app.test_client().websocket(url) as ws:
            state = json.loads(await ws.receive())
            assert state["event"] == "game_state"
            assert state["goal"]["label"] == "Собери кубик 256"
            await ws.send(json.dumps({"type": "move", "direction": "left", "pending_id": "m1"}))
            moved = json.loads(await ws.receive())

    assert moved["daily2048_completed"] is True
    move_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_dailycroc_command_opens_2048_when_admin_switch_is_active() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=77),
        effective_chat=SimpleNamespace(id=77),
        message=SimpleNamespace(message_id=100),
    )
    context = SimpleNamespace(bot=object())

    with (
        patch("app.handlers.daily_crocodile.get_active_daily_game_mode", new_callable=AsyncMock, return_value="2048"),
        patch("app.repos.daily_2048.ensure_puzzle", new_callable=AsyncMock),
        patch("app.handlers.daily_crocodile.repo.get_preference", new_callable=AsyncMock, return_value=None),
        patch("app.handlers.daily_2048.send_daily2048_entry", new_callable=AsyncMock) as send_mock,
    ):
        from app.handlers import daily_crocodile

        await daily_crocodile.dailycroc_command.__wrapped__(update, context)

    send_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_daily_game_command_updates_active_mode() -> None:
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(args=["2048"])
    original = cmd_admin.set_daily_game_command.__wrapped__

    with patch("app.handlers.cmd_admin.set_global_setting", new_callable=AsyncMock) as set_mock:
        await original(update, context)

    set_mock.assert_awaited_once_with(repo.DAILY_GAME_MODE_SETTING_KEY, "2048")
    assert "2048" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_monthly_champions_callback_lists_best_player_per_day() -> None:
    query = SimpleNamespace(
        data="daily2048:month:2026-06",
        answer=AsyncMock(),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=77))
    context = SimpleNamespace()

    with patch(
        "app.handlers.daily_2048.repo.get_monthly_champions",
        new_callable=AsyncMock,
        return_value=[
            {"puzzle_date": date(2026, 6, 1), "display_name": "Mira", "final_score": 6400, "moves": 38},
            {"puzzle_date": date(2026, 6, 2), "display_name": "Nika", "final_score": 6120, "moves": 42},
        ],
    ):
        await daily_2048_handler.monthly_champions_callback(update, context)

    query.answer.assert_awaited_once()
    body = query.message.reply_text.await_args.args[0]
    assert "01.06" in body
    assert "Mira" in body
    assert "6120" in body


@pytest.mark.asyncio
async def test_admin_daily2048_api_lists_and_saves_custom_puzzle() -> None:
    puzzle = repo.Daily2048Puzzle(
        puzzle_date=date(2026, 6, 4),
        board=[[0, 0, 0, 0], [0, 0, 0, 0], [2, 4, 8, 16], [32, 64, 128, 0]],
        goal_type="tile",
        goal_value=512,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="custom",
        par_moves=72,
        target_seconds=240,
        status="ready",
        solution_moves="left,right",
        prepared_at=datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
    )

    with (
        patch("app.web._is_authenticated", return_value=True),
        patch("app.web.daily_2048_repo.list_puzzles", new_callable=AsyncMock, return_value=[puzzle]) as list_mock,
        patch("app.web.daily_2048_repo.upsert_puzzle", new_callable=AsyncMock, return_value=puzzle) as upsert_mock,
    ):
        client = quart_app.test_client()
        listed = await client.get("/api/admin/daily2048")
        saved = await client.post(
            "/api/admin/daily2048/puzzle",
            json={
                "date": "2026-06-04",
                "board": puzzle.board,
                "goal_type": "tile",
                "goal_value": 512,
                "spawn_sequence": puzzle.spawn_sequence,
                "seed": "custom",
                "par_moves": 72,
                "target_seconds": 240,
                "status": "ready",
                "solution_moves": "left,right",
            },
        )

    assert listed.status_code == 200
    listed_body = await listed.get_json()
    assert listed_body["puzzles"][0]["goal_label"] == "Собери кубик 512"
    assert saved.status_code == 200
    list_mock.assert_awaited_once()
    upsert_mock.assert_awaited_once()
    assert upsert_mock.await_args.kwargs["goal_value"] == 512


@pytest.mark.asyncio
async def test_admin_daily_mode_api_switches_to_2048() -> None:
    with (
        patch("app.web._is_authenticated", return_value=True),
        patch("app.web.set_global_setting", new_callable=AsyncMock) as set_mock,
    ):
        response = await quart_app.test_client().post("/api/admin/daily-mode", json={"mode": "2048"})

    assert response.status_code == 200
    set_mock.assert_awaited_once_with(repo.DAILY_GAME_MODE_SETTING_KEY, "2048")


def test_daily2048_template_has_compact_goal_theme_cycle_and_motion_hooks() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "grid-template-rows: auto auto auto minmax(0, 1fr) auto" in template
    assert 'id="theme-btn"' in template
    assert "const THEMES = [" in template
    for theme in ("aero", "desk", "swiss", "botanical", "deco"):
        assert f"id: '{theme}'" in template
        assert f'body[data-theme="{theme}"]' in template

    assert "buildTileModels" in template
    assert "lastMoveDirection" in template
    assert ".tile.moved" in template
    assert ".tile.merged" in template
    assert ".tile.spawn" in template

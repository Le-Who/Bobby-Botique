"""Real commits prove recovery cannot acquire or regain ranked provenance."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.database import db_manager
from app.games import daily_2048, daily_2048_telegram
from app.repos import daily_2048 as repo
from app.web import quart_app
from tests.factories import make_valid_init_data

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


async def test_authenticated_websocket_recovery_persists_unranked_completion(db_conn, monkeypatch):
    # Auth, move persistence, completion ranking and reconnect all use real SQL.
    day = repo.today_puzzle_date()
    puzzle = await repo.upsert_puzzle(
        day,
        board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="pg-authenticated-recovery",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    await repo.get_or_create_result(8111777, puzzle)
    await db_conn.execute("UPDATE users SET is_authorized=1 WHERE user_id=$1", 8111777)
    monkeypatch.setattr(db_manager, "_user_auth_cache", {})
    monkeypatch.setattr("app.web_miniapp.settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="test-token"))
    monkeypatch.setattr("app.bot_instance.get_bot", lambda: None)
    url = "/webapp/daily2048/ws?initData=" + quote(make_valid_init_data("test-token", user_id=8111777))
    async with quart_app.test_client().websocket(url) as ws:
        initial = json.loads(await ws.receive())
        assert initial["recordable"] is True
        await ws.send(
            json.dumps(
                {
                    "type": "move",
                    "direction": "left",
                    "pending_id": "pg-ranked-provenance",
                    "client_elapsed_ms": 0,
                    "client_board_before": [[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                    "client_board_after": [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
                }
            )
        )
        completion = json.loads(await ws.receive())
        assert completion["status"] == "won"
        assert completion["recordable"] is False
        assert completion["final_score"] == 4880
        assert completion["rank"] is None
        assert completion["leaderboard"] == []
        assert completion["board"] == [[256, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]

    stored = await repo.get_result(8111777, day)
    assert stored.recordable is False
    assert await repo.get_rank(8111777, day) is None
    assert await repo.get_leaderboard(day) == []
    async with quart_app.test_client().websocket(url) as ws:
        resumed = json.loads(await ws.receive())
        completion = json.loads(await ws.receive())
        assert resumed["can_practice"] is True
        assert resumed["recordable"] is False
        assert completion["event"] == "daily2048_completed"
        assert completion["recordable"] is False
        assert completion["rank"] is None
        assert completion["final_score"] == 4880


async def test_fabricated_higher_win_is_persisted_but_excluded_from_actual_rankings(db_conn):
    puzzle = await repo.upsert_puzzle(
        DAY,
        board=[[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="pg-ranked-integrity",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    await repo.get_or_create_result(8111777, puzzle)
    await repo.get_or_create_result(8111778, puzzle)

    trusted = await daily_2048.process_move(8111778, "left", now=NOW, client_elapsed_ms=0)
    attack = await daily_2048.process_move(
        8111777,
        "left",
        now=NOW,
        client_elapsed_ms=0,
        client_board_before=[[1024, 1024, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        client_board_after=[[2048, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
    )
    assert trusted["final_score"] == 4880
    assert trusted["recordable"] is True
    assert attack["final_score"] == 5648
    assert attack["board"] == [[2048, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    assert attack["status"] == "won"

    # Before the fix this higher fabricated score takes rank 1.
    assert await repo.get_rank(8111777, DAY) is None
    assert await repo.get_rank(8111778, DAY) == 1
    leaderboard = await repo.get_leaderboard(DAY)
    assert [(row["user_id"], row["final_score"]) for row in leaderboard] == [(8111778, 4880)]
    champions = await repo.get_monthly_champions(2026, 10)
    assert [(row["user_id"], row["final_score"]) for row in champions] == [(8111778, 4880)]
    stored = await repo.get_result(8111777, DAY)
    assert stored.recordable is False
    assert stored.status == "won"
    assert stored.final_score == 5648
    assert stored.won_at is not None and stored.finished_at is not None
    persisted = await db_conn.fetchrow(
        "SELECT status, recordable, final_score FROM daily_2048_results WHERE user_id=$1 AND puzzle_date=$2",
        8111777,
        DAY,
    )
    assert tuple(persisted) == ("won", False, 5648)
    reread = await repo.get_or_create_result(8111777, puzzle)
    assert reread.recordable is False
    completion = await daily_2048_telegram.render_completion_event(8111777, DAY)
    assert completion["rank"] is None
    assert [row["user_id"] for row in completion["leaderboard"]] == [8111778]
    body, _ = await daily_2048_telegram.render_result_body(8111777, DAY)
    assert "5648" in body
    assert "Место дня" not in body
    assert "не участвует в рейтинге" in body


async def test_recovered_active_result_stays_unranked_after_reload_and_ordinary_move(db_conn):
    puzzle = await repo.upsert_puzzle(
        DAY,
        board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}, {"x": 1, "y": 3, "value": 2}],
        seed="pg-lag-recovery",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    await repo.get_or_create_result(8111777, puzzle)
    await db_conn.execute(
        "UPDATE daily_2048_results SET started_at=$1 WHERE user_id=$2 AND puzzle_date=$3",
        datetime(2026, 10, 9, 8, 0, tzinfo=UTC),
        8111777,
        DAY,
    )
    recovered = await daily_2048.process_move(
        8111777,
        "left",
        now=NOW,
        client_elapsed_ms=25_000,
        client_board_before=[[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        client_board_after=[[4, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
    )
    assert recovered["board"] == [[4, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    assert recovered["recordable"] is False
    reloaded = await repo.get_or_create_result(8111777, puzzle)
    assert reloaded.recordable is False
    assert reloaded.elapsed_ms == 25_000

    later = await daily_2048.process_move(8111777, "up", now=NOW, client_elapsed_ms=26_000)
    assert later["board"] == [[4, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 2, 0, 0]]
    assert later["recordable"] is False
    assert later["elapsed_ms"] == 26_000
    assert later["moves"] == 2
    assert (await repo.get_result(8111777, DAY)).recordable is False


async def test_database_update_cannot_promote_previously_unranked_result(db_conn):
    puzzle = await repo.upsert_puzzle(
        DAY,
        board=[[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="tile",
        goal_value=256,
        spawn_sequence=[],
        seed="pg-sticky-recordable",
        par_moves=12,
        target_seconds=180,
        status="ready",
    )
    await repo.get_or_create_result(8111777, puzzle)
    await db_conn.execute("UPDATE daily_2048_results SET recordable=FALSE WHERE user_id=$1", 8111777)
    updated = await repo.update_result_after_move(
        user_id=8111777,
        puzzle_date=DAY,
        board=[[0, 0, 0, 2], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]],
        spawn_index=1,
        moves=1,
        merge_score=0,
        elapsed_ms=1000,
        status="active",
        final_score=0,
        won=False,
        finished=False,
        recordable=True,
    )
    assert updated.recordable is False
    assert (await repo.get_result(8111777, DAY)).recordable is False

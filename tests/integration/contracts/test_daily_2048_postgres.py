"""Real commits prove recovery cannot acquire or regain ranked provenance."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import asyncpg
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
        expected_revision=0,
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
        expected_revision=0,
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
        expected_revision=0,
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


def _gameplay(puzzle):
    return {
        key: getattr(puzzle, key)
        for key in (
            "board",
            "goal_type",
            "goal_value",
            "spawn_sequence",
            "seed",
            "par_moves",
            "target_seconds",
            "solution_moves",
        )
    }


async def _save(conn, puzzle, expected_revision):
    return await repo.upsert_puzzle(
        puzzle.puzzle_date, conn=conn, expected_revision=expected_revision, status=puzzle.status, **_gameplay(puzzle)
    )


@pytest.fixture
async def racing_database(contract_pool):
    """Independent committed connections, with UUID-scoped fixture cleanup."""

    pool = contract_pool
    token = uuid4().int
    day = date.fromordinal(date(2300, 1, 1).toordinal() + token % 100000)
    user_id = 800000000000000000 + token % 100000000000000000
    candidate = repo.Daily2048Puzzle(
        day,
        [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        "tile",
        256,
        spawn_sequence=[{"x": 3, "y": 3, "value": 2}],
        seed="original",
        par_moves=12,
        target_seconds=180,
        solution_moves="left,right",
    )
    try:
        async with pool.acquire() as setup:
            await setup.execute("INSERT INTO public.users(user_id,is_authorized) VALUES($1,0)", user_id)
        yield SimpleNamespace(pool=pool, puzzle=candidate, user_id=user_id)
    finally:
        async with pool.acquire() as cleanup:
            await cleanup.execute("DELETE FROM public.daily_2048_puzzles WHERE puzzle_date=$1", day)
            await cleanup.execute("DELETE FROM public.users WHERE user_id=$1", user_id)


async def _wait_until_blocked(observer, contender_pid, blocker_pid):
    # A scheduling delay does not prove a race: require PostgreSQL itself to
    # report that the contender is waiting on the winner's transaction lock.
    async with asyncio.timeout(5):
        while True:
            blockers = await observer.fetchval("SELECT pg_blocking_pids($1)", contender_pid)
            if blocker_pid in blockers:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("winner", ["start", "edit"])
async def test_start_and_edit_are_serialized_on_two_connections(racing_database, winner):
    env = racing_database
    original = env.puzzle
    edited = replace(
        original,
        board=[[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        goal_type="total",
        goal_value=512,
        spawn_sequence=[{"x": 2, "y": 2, "value": 4}],
        seed="edited",
        par_moves=30,
        target_seconds=300,
        solution_moves="up,down",
    )
    async with env.pool.acquire() as first, env.pool.acquire() as second:
        await _save(first, original, 0)
        first_pid, second_pid = first.get_server_pid(), second.get_server_pid()
        barrier = asyncio.Event()

        async def contender():
            barrier.set()
            if winner == "start":
                return await _save(second, edited, 1)
            return await repo.start_puzzle(env.user_id, original, conn=second)

        pending = None
        try:
            async with first.transaction():
                if winner == "start":
                    locked_puzzle, result = await repo.start_puzzle(env.user_id, original, conn=first)
                else:
                    locked_puzzle = await _save(first, edited, 1)
                pending = asyncio.create_task(contender())
                await barrier.wait()
                await _wait_until_blocked(first, second_pid, first_pid)
            if winner == "start":
                with pytest.raises(repo.DailyPuzzleConflict) as raised:
                    await pending
                assert raised.value.reason == "puzzle_in_use"
                assert result.board == original.board
                assert _gameplay(locked_puzzle) == _gameplay(original)
            else:
                locked_puzzle, result = await pending
                assert locked_puzzle.revision == 2
                assert _gameplay(locked_puzzle) == _gameplay(edited)
                assert result.board == [[128, 128, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
            stored = await repo.get_puzzle(original.puzzle_date)
            assert _gameplay(stored) == _gameplay(original if winner == "start" else edited)
            assert (await repo.get_result(env.user_id, original.puzzle_date)).board == result.board
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("admin_operation", ["status", "edit"])
async def test_independent_players_progress_while_admin_waits_on_shared_puzzle_lock(racing_database, admin_operation):
    env = racing_database
    other_user = env.user_id + 1
    board_a = [[4, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 2]]
    board_b = [[0, 0, 0, 4], [0, 0, 0, 0], [0, 0, 0, 0], [2, 0, 0, 0]]
    pending_admin = None
    async with (
        env.pool.acquire() as player_a,
        env.pool.acquire() as player_b,
        env.pool.acquire() as admin,
        env.pool.acquire() as observer,
    ):
        try:
            await _save(observer, env.puzzle, 0)
            await repo.start_puzzle(env.user_id, env.puzzle, conn=player_a)
            await repo.start_puzzle(other_user, env.puzzle, conn=player_b)

            async def write_move(conn, user_id, board):
                return await repo.update_result_after_move(
                    user_id=user_id,
                    puzzle_date=env.puzzle.puzzle_date,
                    board=board,
                    spawn_index=1,
                    moves=1,
                    merge_score=4,
                    elapsed_ms=1000,
                    status="active",
                    final_score=0,
                    won=False,
                    finished=False,
                    conn=conn,
                )

            async def independent_player():
                # All three player operations must complete while A retains
                # its puzzle lock and uncommitted write on a different result.
                _, started = await repo.start_puzzle(other_user, env.puzzle, conn=player_b)
                assert started.board == env.puzzle.board
                moved = await write_move(player_b, other_user, board_b)
                assert moved.board == board_b and moved.moves == 1
                return await repo.update_result_elapsed(
                    user_id=other_user,
                    puzzle_date=env.puzzle.puzzle_date,
                    elapsed_ms=2000,
                    conn=player_b,
                )

            admin_started = asyncio.Event()

            async def mutate_puzzle():
                admin_started.set()
                if admin_operation == "status":
                    return await repo.set_puzzle_status(
                        env.puzzle.puzzle_date,
                        status="disabled",
                        expected_revision=1,
                        conn=admin,
                    )
                return await _save(admin, replace(env.puzzle, seed="forbidden-after-play"), 1)

            async with player_a.transaction():
                await write_move(player_a, env.user_id, board_a)
                other_result = await asyncio.wait_for(independent_player(), timeout=3)
                assert other_result.board == board_b and other_result.elapsed_ms == 2000
                # Require actual independent progress before queueing the
                # writer, since PostgreSQL fairly queues conflicting waiters.
                pending_admin = asyncio.create_task(mutate_puzzle())
                await admin_started.wait()
                await _wait_until_blocked(observer, admin.get_server_pid(), player_a.get_server_pid())
                assert (
                    await observer.fetchval(
                        "SELECT status FROM public.daily_2048_puzzles WHERE puzzle_date=$1", env.puzzle.puzzle_date
                    )
                    == "ready"
                )
            if admin_operation == "status":
                assert (await pending_admin).status == "disabled"
            else:
                with pytest.raises(repo.DailyPuzzleConflict, match="puzzle_in_use"):
                    await pending_admin
            assert (await repo.get_result(env.user_id, env.puzzle.puzzle_date)).board == board_a
            assert (await repo.get_result(other_user, env.puzzle.puzzle_date)).board == board_b
        finally:
            if pending_admin is not None and not pending_admin.done():
                pending_admin.cancel()
                await asyncio.gather(pending_admin, return_exceptions=True)
            await observer.execute("DELETE FROM public.users WHERE user_id=$1", other_user)


async def test_ensure_loses_insert_race_without_overwriting_manual_puzzle(racing_database):
    env = racing_database
    manual = replace(env.puzzle, status="draft", seed="manual-winner", goal_value=1024)
    pending = None
    async with env.pool.acquire() as admin, env.pool.acquire() as observer:
        try:
            async with admin.transaction():
                await _save(admin, manual, 0)
                pending = asyncio.create_task(repo.ensure_puzzle(manual.puzzle_date))
                # The third pool connection executes ensure. Its conditional
                # insert must wait for the admin's still-uncommitted unique key.
                async with asyncio.timeout(5):
                    while not await observer.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)))",
                        admin.get_server_pid(),
                    ):
                        await asyncio.sleep(0.01)
            ensured = await pending
            assert ensured.status == "draft" and ensured.revision == 1
            assert _gameplay(ensured) == _gameplay(manual)
            stored = await repo.get_puzzle(manual.puzzle_date)
            assert stored.seed == "manual-winner" and stored.goal_value == 1024
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("status", ["draft", "disabled"])
async def test_unavailable_status_rejects_start_without_inserting_result(db_conn, status):
    puzzle = repo.Daily2048Puzzle(
        DAY, [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "tile", 256, status=status
    )
    saved = await _save(db_conn, puzzle, 0)
    with pytest.raises(repo.DailyPuzzleUnavailable) as raised:
        await repo.start_puzzle(8111777, saved, conn=db_conn)
    assert raised.value.reason == status
    assert await db_conn.fetchval("SELECT COUNT(*) FROM public.daily_2048_results WHERE puzzle_date=$1", DAY) == 0


async def test_disabled_move_and_status_restore_preserve_entire_played_result(db_conn):
    puzzle = repo.Daily2048Puzzle(DAY, [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "tile", 256)
    saved = await _save(db_conn, puzzle, 0)
    await repo.start_puzzle(8111777, saved, conn=db_conn)
    before = dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY))
    disabled = await repo.set_puzzle_status(DAY, status="disabled", expected_revision=1, conn=db_conn)
    with pytest.raises(repo.DailyPuzzleUnavailable):
        await daily_2048.process_move(8111777, "left", now=NOW)
    assert dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY)) == before
    restored = await repo.set_puzzle_status(DAY, status="ready", expected_revision=disabled.revision, conn=db_conn)
    reread = await repo.get_or_create_result(8111777, restored)
    assert reread.board == before["board"] and reread.started_at == before["started_at"]
    assert _gameplay(restored) == _gameplay(saved)


async def test_revision_migration_is_idempotent_and_does_not_rewrite_results(db_conn):
    puzzle = repo.Daily2048Puzzle(DAY, [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "tile", 256)
    saved = await _save(db_conn, puzzle, 0)
    await repo.start_puzzle(8111777, saved, conn=db_conn)
    # The fixture owns a generated clone. Reconstruct the legacy column shape
    # only there, without changing the source test database/template.
    await db_conn.execute("ALTER TABLE public.daily_2048_puzzles DROP COLUMN revision")
    await db_conn.execute(
        "UPDATE public.daily_2048_results SET status='won', final_score=1234, won_at=NOW() WHERE puzzle_date=$1", DAY
    )
    before_puzzle = dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_puzzles WHERE puzzle_date=$1", DAY))
    before_result = dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY))
    before_leaderboard = await repo.get_leaderboard(DAY)
    migration = (Path(__file__).resolve().parents[3] / "scripts/migrations/075_daily_2048_edit_revision.sql").read_text(
        encoding="utf-8"
    )
    await db_conn.execute(migration)
    await db_conn.execute(migration)
    after_puzzle = dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_puzzles WHERE puzzle_date=$1", DAY))
    assert after_puzzle.pop("revision") == 1
    assert after_puzzle == before_puzzle
    assert (
        dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY))
        == before_result
    )
    assert await repo.get_leaderboard(DAY) == before_leaderboard
    with pytest.raises(asyncpg.CheckViolationError):
        async with db_conn.transaction():
            await db_conn.execute("UPDATE public.daily_2048_puzzles SET revision=0 WHERE puzzle_date=$1", DAY)


async def test_disable_between_ready_read_and_move_write_preserves_result(db_conn, monkeypatch):
    puzzle = repo.Daily2048Puzzle(DAY, [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "tile", 256)
    saved = await _save(db_conn, puzzle, 0)
    await repo.start_puzzle(8111777, saved, conn=db_conn)
    before = dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY))
    ready_read, disabled = asyncio.Event(), asyncio.Event()
    real_read = daily_2048.get_daily_state

    async def read_then_wait(*args, **kwargs):
        state = await real_read(*args, **kwargs)
        ready_read.set()
        await disabled.wait()
        return state

    monkeypatch.setattr(daily_2048, "get_daily_state", read_then_wait)
    move = asyncio.create_task(daily_2048.process_move(8111777, "left", now=NOW))
    try:
        await asyncio.wait_for(ready_read.wait(), timeout=5)
        await repo.set_puzzle_status(DAY, status="disabled", expected_revision=1, conn=db_conn)
        disabled.set()
        with pytest.raises(repo.DailyPuzzleUnavailable, match="disabled"):
            await move
        assert (
            dict(await db_conn.fetchrow("SELECT * FROM public.daily_2048_results WHERE puzzle_date=$1", DAY)) == before
        )
    finally:
        disabled.set()
        if not move.done():
            move.cancel()
        await asyncio.gather(move, return_exceptions=True)


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
        expected_revision=0,
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

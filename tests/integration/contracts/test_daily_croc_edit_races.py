"""Actual PostgreSQL locks and ticket fences in an owned disposable clone."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.repos import crocodile_daily as repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
DAY = date(2027, 2, 5)
USER = 8221777


async def seed(conn, *, hints=None):
    await conn.execute("INSERT INTO public.crocodile_daily_days(puzzle_date) VALUES($1)", DAY)
    await conn.execute(
        """INSERT INTO public.crocodile_daily_puzzles(puzzle_date,difficulty,target_word,topic,lang,hints,image_prompt)
           VALUES($1,'easy','кот','Животные','ru',$2::jsonb,'cat')""",
        DAY,
        ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"] if hints is None else hints,
    )
    return await repo.get_puzzle(DAY, conn=conn)


async def blocked(observer, contender, blocker):
    async with asyncio.timeout(5):
        while blocker.get_server_pid() not in await observer.fetchval(
            "SELECT pg_blocking_pids($1)", contender.get_server_pid()
        ):
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("winner", ["start", "reset"])
async def test_start_and_reset_share_row_fence(contract_pool, monkeypatch, winner):
    monkeypatch.setattr(repo, "_prepare_daily_word_candidate", AsyncCandidate())
    async with contract_pool.acquire() as first, contract_pool.acquire() as second, contract_pool.acquire() as observer:
        await seed(first)
        pending = None
        try:
            async with first.transaction():
                if winner == "start":
                    await repo.start_puzzle(USER, DAY, conn=first)
                    pending = asyncio.create_task(repo.regenerate_puzzle_word(DAY, expected_revision=1))
                    # Reset acquires a separate connection from the same real pool.
                    async with asyncio.timeout(5):
                        while not await observer.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE $1=ANY(pg_blocking_pids(pid)))",
                            first.get_server_pid(),
                        ):
                            await asyncio.sleep(0.01)
                else:
                    await repo._regenerate_puzzle_word_on_conn(
                        first, DAY, "easy", 1, ("собака", "ru", "Животные", True)
                    )
                    pending = asyncio.create_task(repo.start_puzzle(USER, DAY, conn=second))
                    await blocked(observer, second, first)
            if winner == "start":
                with pytest.raises(repo.DailyCrocConflict, match="puzzle_in_use"):
                    await pending
                assert (await repo.get_result(USER, DAY)).status == "active"
                assert (await repo.get_puzzle(DAY)).target_word == "кот"
            else:
                with pytest.raises(repo.DailyCrocNotReady):
                    await pending
                assert (
                    await observer.fetchval(
                        "SELECT COUNT(*) FROM public.crocodile_daily_results WHERE puzzle_date=$1", DAY
                    )
                    == 0
                )
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


class AsyncCandidate:
    async def __call__(self, *args, **kwargs):
        return "собака", "ru", "Животные", True


@pytest.mark.parametrize("winner", ["edit", "finish"])
async def test_content_edit_and_finish_are_atomic_across_connections(contract_pool, winner):
    async with contract_pool.acquire() as first, contract_pool.acquire() as second, contract_pool.acquire() as observer:
        await seed(first)
        ticket = await repo.begin_preparation(DAY, "easy", 1, conn=first)
        pending = None
        try:
            async with first.transaction():
                if winner == "edit":
                    await repo.edit_puzzle(DAY, "easy", 1, image_prompt="new", conn=first)
                    pending = asyncio.create_task(
                        repo.finish_preparation(ticket, {"image_file_id": "old-art"}, conn=second)
                    )
                else:
                    assert await repo.finish_preparation(ticket, {"image_file_id": "old-art"}, conn=first)
                    pending = asyncio.create_task(repo.edit_puzzle(DAY, "easy", 1, image_prompt="new", conn=second))
                await blocked(observer, second, first)
            outcome = await pending
            if winner == "edit":
                assert outcome is False
            else:
                assert outcome.content_revision == 2
            puzzle = await repo.get_puzzle(DAY)
            assert puzzle.image_prompt == "new" and puzzle.image_file_id == "" and puzzle.prepared_at is None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_competing_generations_and_duplicate_finish_only_accept_current_ticket(contract_pool):
    async with contract_pool.acquire() as first, contract_pool.acquire() as second:
        await seed(first)
        tickets = await asyncio.gather(
            repo.begin_preparation(DAY, "easy", 1, conn=first), repo.begin_preparation(DAY, "easy", 1, conn=second)
        )
        current_token = (await repo.get_puzzle(DAY)).generation_token
        current = next(ticket for ticket in tickets if ticket.token == current_token)
        old = next(ticket for ticket in tickets if ticket.token != current_token)
        assert not await repo.set_puzzle_hints(old, ["stale"], conn=first)
        assert not await repo.set_puzzle_image_prompt(old, "stale", conn=first)
        assert not await repo.set_puzzle_image_asset(old, "stale", conn=first)
        accepted = await asyncio.gather(
            repo.finish_preparation(current, {"image_file_id": "current"}, conn=first),
            repo.finish_preparation(current, {"image_file_id": "duplicate"}, conn=second),
        )
        assert sorted(accepted) == [False, True]
        puzzle = await repo.get_puzzle(DAY)
        assert puzzle.hints == ["Домашний любимец", "Усатая мордочка", "Мягкие лапки"] and puzzle.image_prompt == "cat"
        assert puzzle.image_file_id in {"current", "duplicate"}
        assert puzzle.generation_token is None and puzzle.prepared_at is not None


async def test_revision_migration_backfills_without_rewriting_results(db_conn):
    await seed(db_conn)
    await repo.start_puzzle(USER, DAY, conn=db_conn)
    before_result = dict(await db_conn.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER))
    await db_conn.execute(
        "ALTER TABLE public.crocodile_daily_puzzles DROP COLUMN content_revision,DROP COLUMN generation_token"
    )
    before = dict(await db_conn.fetchrow("SELECT * FROM public.crocodile_daily_puzzles WHERE puzzle_date=$1", DAY))
    sql = (Path(__file__).resolve().parents[3] / "scripts/migrations/076_daily_croc_generation_revision.sql").read_text(
        encoding="utf-8"
    )
    await db_conn.execute(sql)
    await db_conn.execute(sql)
    after = dict(await db_conn.fetchrow("SELECT * FROM public.crocodile_daily_puzzles WHERE puzzle_date=$1", DAY))
    assert after.pop("content_revision") == 1 and after.pop("generation_token") is None
    assert after == before
    assert (
        dict(await db_conn.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER))
        == before_result
    )


async def test_played_missing_prompt_is_filled_once_by_current_ticket_without_result_changes(contract_pool):
    async with contract_pool.acquire() as first, contract_pool.acquire() as second:
        await seed(first)
        await first.execute("UPDATE public.crocodile_daily_puzzles SET image_prompt='' WHERE puzzle_date=$1", DAY)
        _, original = await repo.start_puzzle(USER, DAY, conn=first)
        before = dict(await first.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER))
        ticket = await repo.begin_preparation(DAY, "easy", 1, conn=first)
        results = await asyncio.gather(
            repo.set_puzzle_image_prompt(ticket, "cat one", conn=first),
            repo.set_puzzle_image_prompt(ticket, "cat two", conn=second),
        )
        assert sorted(results) == [False, True]
        replacement = await repo.begin_preparation(DAY, "easy", 1, conn=second)
        assert not await repo.set_puzzle_image_prompt(ticket, "stale", conn=first)
        assert not await repo.set_puzzle_hints(
            replacement, ["replaced one", "replaced two", "replaced three"], conn=second
        )
        assert await repo.finish_preparation(replacement, conn=second)
        puzzle, reopened = await repo.start_puzzle(USER, DAY, conn=first)
        assert reopened == original
        assert puzzle.image_prompt in {"cat one", "cat two"}
        assert (
            dict(await first.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER)) == before
        )


@pytest.mark.parametrize("writer", ["asset", "finish"])
async def test_played_missing_art_accepts_one_fill_and_freezes_identity(contract_pool, writer):
    async with contract_pool.acquire() as first, contract_pool.acquire() as second, contract_pool.acquire() as observer:
        await seed(first)
        await repo.start_puzzle(USER, DAY, conn=first)
        result_before = dict(
            await first.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER)
        )
        ticket = await repo.begin_preparation(DAY, "easy", 1, conn=first)

        async def fill(conn, image):
            if writer == "asset":
                return await repo.set_puzzle_image_asset(ticket, image, conn=conn)
            return await repo.finish_preparation(ticket, {"image_file_id": image}, conn=conn)

        pending = None
        try:
            async with first.transaction():
                assert await fill(first, "first-art")
                pending = asyncio.create_task(fill(second, "competing-art"))
                await blocked(observer, second, first)
            assert await pending is False
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        current = await repo.begin_preparation(DAY, "easy", 1, conn=second)
        frozen = dict(await first.fetchrow("SELECT * FROM public.crocodile_daily_puzzles WHERE puzzle_date=$1", DAY))
        assert not await repo.set_puzzle_image_asset(current, "replacement", conn=first)
        assert not await repo.clear_puzzle_image_asset(current, expected_file_id="first-art", conn=second)
        assert not await repo.finish_preparation(current, {"image_file_id": "replacement"}, conn=first)
        assert not await repo.finish_preparation(current, {"image_model": "another-model"}, conn=second)
        assert (
            dict(await first.fetchrow("SELECT * FROM public.crocodile_daily_puzzles WHERE puzzle_date=$1", DAY))
            == frozen
        )
        assert (
            dict(await first.fetchrow("SELECT * FROM public.crocodile_daily_results WHERE user_id=$1", USER))
            == result_before
        )

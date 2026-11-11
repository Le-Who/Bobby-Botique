"""A rejected progress write must not look like an accepted game action."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from app.repos import crocodile_daily as croc
from app.repos import daily_2048 as tiles

pytestmark = pytest.mark.asyncio
DAY = date(2026, 11, 11)


async def test_crocodile_rejected_progress_write_is_a_conflict(monkeypatch):
    current = croc.DailyResult(7, DAY, "active", [], 0.0, 0, None, None, 0, "", 0)
    monkeypatch.setattr(croc, "get_or_create_result", AsyncMock(return_value=current))
    write = AsyncMock(return_value=[])
    monkeypatch.setattr(croc.db, "db_query", write)
    with pytest.raises(RuntimeError, match="progress_conflict"):
        await croc.append_attempt_and_maybe_finish(
            user_id=7, puzzle_date=DAY, attempt={"word": "stone", "status": "wrong", "score": 0.0}
        )
    assert "attempts = $12::jsonb" in write.await_args.args[0]
    assert write.await_args.args[1][-2:] == ([], 0)


async def test_2048_rejected_active_progress_write_is_a_conflict(monkeypatch):
    board = [[2, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
    current = tiles.Daily2048Result(7, DAY, "active", board, 1, 1, 0, 0, 100, datetime.now(UTC), None, None)

    @asynccontextmanager
    async def transaction(_conn=None):
        yield object()

    monkeypatch.setattr(tiles, "_transaction", transaction)
    monkeypatch.setattr(tiles, "_locked_puzzle", AsyncMock(return_value=tiles.Daily2048Puzzle(DAY, board, "tile", 256)))
    write = AsyncMock(side_effect=[[], [asdict(current)]])
    monkeypatch.setattr(tiles.db, "db_query", write)
    with pytest.raises(RuntimeError, match="progress_conflict"):
        await tiles.update_result_after_move(
            user_id=7,
            puzzle_date=DAY,
            board=board,
            spawn_index=1,
            moves=1,
            merge_score=0,
            elapsed_ms=100,
            status="active",
            final_score=0,
            won=False,
            finished=False,
        )
    assert "moves = $14" in write.await_args_list[0].args[0]
    assert write.await_args_list[0].args[1][-1] == 0

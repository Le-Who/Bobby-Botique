from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.repos import daily_2048 as repo

Direction = str
Board = list[list[int]]

_DIRECTION_ALIASES = {
    "left": "left",
    "right": "right",
    "up": "up",
    "down": "down",
    "l": "left",
    "r": "right",
    "u": "up",
    "d": "down",
}


@dataclass(frozen=True)
class MoveOutcome:
    board: Board
    moved: bool
    gained_score: int


@dataclass(frozen=True)
class SpawnOutcome:
    board: Board
    spawn_index: int
    spawned: dict[str, int] | None


def _clone_board(board: Board) -> Board:
    return [list(row) for row in repo.normalize_board(board)]


def _normalize_direction(direction: str | None) -> str:
    normalized = (direction or "").strip().lower()
    if normalized not in _DIRECTION_ALIASES:
        raise ValueError(f"unsupported direction: {direction!r}")
    return _DIRECTION_ALIASES[normalized]


def _merge_line(values: list[int]) -> tuple[list[int], int]:
    compact = [value for value in values if value > 0]
    merged: list[int] = []
    score = 0
    index = 0
    while index < len(compact):
        current = compact[index]
        if index + 1 < len(compact) and compact[index + 1] == current:
            new_value = current * 2
            merged.append(new_value)
            score += new_value
            index += 2
        else:
            merged.append(current)
            index += 1
    merged.extend([0] * (repo.DEFAULT_BOARD_SIZE - len(merged)))
    return merged[: repo.DEFAULT_BOARD_SIZE], score


def _transpose(board: Board) -> Board:
    return [[board[y][x] for y in range(repo.DEFAULT_BOARD_SIZE)] for x in range(repo.DEFAULT_BOARD_SIZE)]


def apply_move(board: Board, direction: Direction) -> MoveOutcome:
    direction = _normalize_direction(direction)
    source = _clone_board(board)
    working = _transpose(source) if direction in {"up", "down"} else source
    reverse = direction in {"right", "down"}
    moved_score = 0
    moved_rows: Board = []

    for row in working:
        raw = list(reversed(row)) if reverse else list(row)
        merged, gained = _merge_line(raw)
        if reverse:
            merged = list(reversed(merged))
        moved_rows.append(merged)
        moved_score += gained

    result = _transpose(moved_rows) if direction in {"up", "down"} else moved_rows
    return MoveOutcome(board=result, moved=result != source, gained_score=moved_score)


def _available_cells(board: Board) -> list[tuple[int, int]]:
    return [
        (x, y) for y, row in enumerate(repo.normalize_board(board)) for x, value in enumerate(row) if int(value) == 0
    ]


def _fallback_spawn(spawn_index: int, seed: str, available: list[tuple[int, int]]) -> dict[str, int]:
    digest = hashlib.sha256(f"{seed}:{spawn_index}".encode()).digest()
    idx = int.from_bytes(digest[:4], "big") % len(available)
    value = 4 if digest[4] % 10 == 0 else 2
    x, y = available[idx]
    return {"x": x, "y": y, "value": value}


def spawn_tile(
    board: Board,
    spawn_sequence: list[dict[str, int]],
    *,
    spawn_index: int,
    seed: str,
) -> SpawnOutcome:
    next_board = _clone_board(board)
    available = _available_cells(next_board)
    if not available:
        return SpawnOutcome(board=next_board, spawn_index=spawn_index, spawned=None)

    sequence = repo.normalize_spawn_sequence(spawn_sequence)
    candidate = sequence[spawn_index] if 0 <= spawn_index < len(sequence) else None
    if not candidate or (candidate["x"], candidate["y"]) not in available:
        candidate = _fallback_spawn(spawn_index, seed, available)

    next_board[candidate["y"]][candidate["x"]] = candidate["value"]
    return SpawnOutcome(board=next_board, spawn_index=spawn_index + 1, spawned=candidate)


def moves_available(board: Board) -> bool:
    normalized = repo.normalize_board(board)
    if _available_cells(normalized):
        return True
    for y in range(repo.DEFAULT_BOARD_SIZE):
        for x in range(repo.DEFAULT_BOARD_SIZE):
            value = normalized[y][x]
            if x + 1 < repo.DEFAULT_BOARD_SIZE and normalized[y][x + 1] == value:
                return True
            if y + 1 < repo.DEFAULT_BOARD_SIZE and normalized[y + 1][x] == value:
                return True
    return False


def goal_reached(board: Board, puzzle: repo.Daily2048Puzzle) -> bool:
    normalized = repo.normalize_board(board)
    if puzzle.goal_type == "total":
        return sum(value for row in normalized for value in row) >= puzzle.goal_value
    return any(value >= puzzle.goal_value for row in normalized for value in row)


def goal_payload(puzzle: repo.Daily2048Puzzle) -> dict[str, Any]:
    if puzzle.goal_type == "total":
        label = f"Собери стоимость {puzzle.goal_value}"
        help_text = "Сумма всех кубиков на поле должна дойти до цели."
    else:
        label = f"Собери кубик {puzzle.goal_value}"
        help_text = "Слей одинаковые кубики, пока на поле не появится нужное число."
    return {
        "type": puzzle.goal_type,
        "value": puzzle.goal_value,
        "label": label,
        "help": help_text,
    }


def compute_final_score(
    puzzle: repo.Daily2048Puzzle,
    *,
    moves: int,
    elapsed_ms: int,
    merge_score: int,
) -> int:
    base = puzzle.goal_value * 10
    target_ms = max(30, puzzle.target_seconds) * 1000
    speed_bonus = max(0, target_ms - max(0, elapsed_ms)) // 100
    move_bonus = max(0, puzzle.par_moves - max(0, moves)) * 24
    merge_bonus = min(max(0, merge_score), puzzle.goal_value * 4)
    return int(base + speed_bonus + move_bonus + merge_bonus)


async def ensure_prepared_puzzles(*, now: datetime | None = None) -> list[repo.Daily2048Puzzle]:
    start_date = repo.today_puzzle_date(now)
    # ⚡ Perf: was a sequential for-loop calling ensure_puzzle() one-by-one.
    # asyncio.gather() issues all DB queries concurrently — total latency drops
    # from sum(RTTs) to max(RTT), saving ~7× round-trips for the default 7-day window.
    dates = [start_date + timedelta(days=offset) for offset in range(repo.DAILY_2048_PREP_DAYS_AHEAD + 1)]
    return list(await asyncio.gather(*[repo.ensure_puzzle(d) for d in dates]))


async def get_daily_state(
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[repo.Daily2048Puzzle, repo.Daily2048Result]:
    puzzle = await repo.ensure_today_puzzle(now)
    result = await repo.get_or_create_result(user_id, puzzle)
    return puzzle, result


def _elapsed_ms(started_at: datetime, now: datetime) -> int:
    started = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
    current = now if now.tzinfo else now.replace(tzinfo=UTC)
    return max(0, int((current - started).total_seconds() * 1000))


def _resolve_elapsed_ms(
    result: repo.Daily2048Result,
    now: datetime,
    client_elapsed_ms: int | None,
) -> int:
    wall_elapsed_ms = _elapsed_ms(result.started_at, now)
    if client_elapsed_ms is None:
        return max(result.elapsed_ms, wall_elapsed_ms)
    client_elapsed_ms = max(0, int(client_elapsed_ms))
    return min(client_elapsed_ms, wall_elapsed_ms)


def _validated_client_move_outcome(
    direction: str,
    *,
    client_board_before: Any | None,
    client_board_after: Any | None,
) -> MoveOutcome | None:
    if client_board_before is None or client_board_after is None:
        return None
    outcome = apply_move(repo.normalize_board(client_board_before), direction)
    if not outcome.moved:
        return None
    if outcome.board != repo.normalize_board(client_board_after):
        return None
    return outcome


def _event_from_result(
    result: repo.Daily2048Result,
    puzzle: repo.Daily2048Puzzle,
    *,
    moved: bool = True,
    gained_score: int = 0,
    spawned: dict[str, int] | None = None,
    practice: bool = False,
) -> dict[str, Any]:
    public_status = "active" if practice else result.status
    return {
        "event": "move_result",
        "board": result.board,
        "goal": goal_payload(puzzle),
        "status": public_status,
        "moved": moved,
        "gained_score": gained_score,
        "spawned": spawned,
        "spawn_index": result.spawn_index,
        "moves": result.moves,
        "merge_score": result.merge_score,
        "elapsed_ms": result.elapsed_ms,
        "final_score": result.final_score,
        "recordable": bool(result.recordable) and not practice,
        "daily2048_completed": result.status == "won" and not practice,
        "game_over": public_status in {"won", "lost"},
    }


def _practice_base_result(result: repo.Daily2048Result, puzzle: repo.Daily2048Puzzle) -> repo.Daily2048Result:
    if result.status != "lost":
        return result
    return repo.Daily2048Result(
        user_id=result.user_id,
        puzzle_date=result.puzzle_date,
        status="practice",
        board=_clone_board(puzzle.board),
        spawn_index=0,
        moves=0,
        merge_score=0,
        final_score=0,
        elapsed_ms=0,
        started_at=result.started_at,
        won_at=result.won_at,
        finished_at=result.finished_at,
        recordable=False,
    )


async def process_move(
    user_id: int,
    direction: str,
    *,
    now: datetime | None = None,
    client_elapsed_ms: int | None = None,
    client_board_before: Any | None = None,
    client_board_after: Any | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(tz=UTC)
    puzzle, result = await get_daily_state(user_id, now=current_time)
    if result.status != "active":
        return _event_from_result(result, puzzle, moved=False)

    outcome = _validated_client_move_outcome(
        direction,
        client_board_before=client_board_before,
        client_board_after=client_board_after,
    ) or apply_move(result.board, direction)
    if not outcome.moved:
        return _event_from_result(result, puzzle, moved=False)

    spawned = spawn_tile(
        outcome.board,
        puzzle.spawn_sequence,
        spawn_index=result.spawn_index,
        seed=puzzle.seed,
    )
    next_board = spawned.board
    moves = result.moves + 1
    merge_score = result.merge_score + outcome.gained_score
    elapsed_ms = _resolve_elapsed_ms(result, current_time, client_elapsed_ms)
    won = goal_reached(next_board, puzzle)
    lost = not won and not moves_available(next_board)
    status = "won" if won else "lost" if lost else "active"
    final_score = compute_final_score(puzzle, moves=moves, elapsed_ms=elapsed_ms, merge_score=merge_score) if won else 0
    updated = await repo.update_result_after_move(
        user_id=user_id,
        puzzle_date=puzzle.puzzle_date,
        board=next_board,
        spawn_index=spawned.spawn_index,
        moves=moves,
        merge_score=merge_score,
        elapsed_ms=elapsed_ms,
        status=status,
        final_score=final_score,
        won=won,
        finished=won or lost,
    )
    return _event_from_result(
        updated,
        puzzle,
        moved=True,
        gained_score=outcome.gained_score,
        spawned=spawned.spawned,
    )


async def process_practice_move(
    result: repo.Daily2048Result,
    puzzle: repo.Daily2048Puzzle,
    direction: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    del now
    base_result = _practice_base_result(result, puzzle)
    outcome = apply_move(base_result.board, direction)
    if not outcome.moved:
        practice_result = copy.copy(base_result)
        practice_result.recordable = False
        return _event_from_result(practice_result, puzzle, moved=False, practice=True)

    spawned = spawn_tile(
        outcome.board,
        puzzle.spawn_sequence,
        spawn_index=base_result.spawn_index,
        seed=f"{puzzle.seed}:practice",
    )
    practice_result = repo.Daily2048Result(
        user_id=base_result.user_id,
        puzzle_date=base_result.puzzle_date,
        status="practice",
        board=spawned.board,
        spawn_index=spawned.spawn_index,
        moves=base_result.moves + 1,
        merge_score=base_result.merge_score + outcome.gained_score,
        final_score=base_result.final_score,
        elapsed_ms=base_result.elapsed_ms,
        started_at=base_result.started_at,
        won_at=base_result.won_at,
        finished_at=base_result.finished_at,
        recordable=False,
    )
    return _event_from_result(
        practice_result,
        puzzle,
        moved=True,
        gained_score=outcome.gained_score,
        spawned=spawned.spawned,
        practice=True,
    )

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app import database as db
from app.repos import crocodile_daily
from app.repos.settings_repo import get_global_setting
from app.utils.json_compat import json

DAILY_GAME_MODE_SETTING_KEY = "daily_game_mode"
DAILY_GAME_MODE_CROCODILE = "crocodile"
DAILY_GAME_MODE_2048 = "2048"

DAILY_2048_PREP_DAYS_AHEAD = 7
DEFAULT_BOARD_SIZE = 4
DEFAULT_TARGET_SECONDS = 240
DEFAULT_PAR_MOVES = 72
GOAL_TYPES = {"tile", "total"}
DEFAULT_SIGNATURE_LOOKBACK = 730

_SNAKE_PATHS = [
    [(0, 3), (1, 3), (2, 3), (3, 3), (3, 2), (2, 2), (1, 2), (0, 2), (0, 1), (1, 1), (2, 1), (3, 1)],
    [(3, 3), (2, 3), (1, 3), (0, 3), (0, 2), (1, 2), (2, 2), (3, 2), (3, 1), (2, 1), (1, 1), (0, 1)],
    [(0, 0), (0, 1), (0, 2), (0, 3), (1, 3), (1, 2), (1, 1), (1, 0), (2, 0), (2, 1), (2, 2), (2, 3)],
    [(3, 0), (3, 1), (3, 2), (3, 3), (2, 3), (2, 2), (2, 1), (2, 0), (1, 0), (1, 1), (1, 2), (1, 3)],
]

_GOAL_PATTERNS = [
    ("tile", 256, 58, [128, 64, 32, 16, 8, 4, 2, 2]),
    ("tile", 512, 74, [128, 128, 64, 32, 16, 8, 4, 2]),
    ("tile", 512, 78, [256, 64, 32, 16, 8, 4, 4, 2]),
    ("total", 768, 88, [256, 128, 64, 64, 32, 16, 8, 4]),
    ("tile", 256, 54, [128, 64, 64, 32, 16, 8, 4, 2]),
    ("tile", 512, 82, [128, 64, 64, 32, 16, 16, 8, 4]),
]


@dataclass
class Daily2048Puzzle:
    puzzle_date: date
    board: list[list[int]]
    goal_type: str
    goal_value: int
    spawn_sequence: list[dict[str, int]] = field(default_factory=list)
    seed: str = ""
    par_moves: int = DEFAULT_PAR_MOVES
    target_seconds: int = DEFAULT_TARGET_SECONDS
    status: str = "ready"
    solution_moves: str = ""
    prepared_at: datetime | None = None


@dataclass
class Daily2048Result:
    user_id: int
    puzzle_date: date
    status: str
    board: list[list[int]]
    spawn_index: int
    moves: int
    merge_score: int
    final_score: int
    elapsed_ms: int
    started_at: datetime
    won_at: datetime | None
    finished_at: datetime | None
    recordable: bool = True


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def normalize_board(value: Any) -> list[list[int]]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        parsed = []

    board: list[list[int]] = []
    for y in range(DEFAULT_BOARD_SIZE):
        raw_row = parsed[y] if y < len(parsed) and isinstance(parsed[y], list) else []
        row: list[int] = []
        for x in range(DEFAULT_BOARD_SIZE):
            try:
                cell = int(raw_row[x]) if x < len(raw_row) else 0
            except (TypeError, ValueError):
                cell = 0
            row.append(max(0, cell))
        board.append(row)
    return board


def normalize_spawn_sequence(value: Any) -> list[dict[str, int]]:
    parsed = _json_value(value)
    if not isinstance(parsed, list):
        return []
    sequence: list[dict[str, int]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            x = int(item.get("x", 0))
            y = int(item.get("y", 0))
            tile_value = int(item.get("value", 2))
        except (TypeError, ValueError):
            continue
        sequence.append(
            {
                "x": max(0, min(DEFAULT_BOARD_SIZE - 1, x)),
                "y": max(0, min(DEFAULT_BOARD_SIZE - 1, y)),
                "value": tile_value if tile_value in {2, 4, 8} else 2,
            }
        )
    return sequence


def normalize_goal_type(value: str | None) -> str:
    goal_type = (value or "tile").strip().lower()
    return goal_type if goal_type in GOAL_TYPES else "tile"


def board_signature(board: Any) -> str:
    payload = json.dumps(normalize_board(board), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def puzzle_signature(puzzle: Daily2048Puzzle) -> str:
    payload = {
        "board": normalize_board(puzzle.board),
        "goal_type": normalize_goal_type(puzzle.goal_type),
        "goal_value": int(puzzle.goal_value),
        "par_moves": int(puzzle.par_moves),
        "target_seconds": int(puzzle.target_seconds),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def today_puzzle_date(now: datetime | None = None) -> date:
    return crocodile_daily.today_puzzle_date(now)


def _row_to_puzzle(row: Any) -> Daily2048Puzzle:
    return Daily2048Puzzle(
        puzzle_date=_row_get(row, "puzzle_date"),
        board=normalize_board(_row_get(row, "board")),
        goal_type=normalize_goal_type(_row_get(row, "goal_type")),
        goal_value=int(_row_get(row, "goal_value", 512) or 512),
        spawn_sequence=normalize_spawn_sequence(_row_get(row, "spawn_sequence")),
        seed=str(_row_get(row, "seed", "") or ""),
        par_moves=int(_row_get(row, "par_moves", DEFAULT_PAR_MOVES) or DEFAULT_PAR_MOVES),
        target_seconds=int(_row_get(row, "target_seconds", DEFAULT_TARGET_SECONDS) or DEFAULT_TARGET_SECONDS),
        status=str(_row_get(row, "status", "ready") or "ready"),
        solution_moves=str(_row_get(row, "solution_moves", "") or ""),
        prepared_at=_row_get(row, "prepared_at"),
    )


def _row_to_result(row: Any) -> Daily2048Result:
    started_at = _row_get(row, "started_at") or datetime.now(tz=UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return Daily2048Result(
        user_id=int(_row_get(row, "user_id") or 0),
        puzzle_date=_row_get(row, "puzzle_date"),
        status=str(_row_get(row, "status", "active") or "active"),
        board=normalize_board(_row_get(row, "board")),
        spawn_index=int(_row_get(row, "spawn_index", 0) or 0),
        moves=int(_row_get(row, "moves", 0) or 0),
        merge_score=int(_row_get(row, "merge_score", 0) or 0),
        final_score=int(_row_get(row, "final_score", 0) or 0),
        elapsed_ms=int(_row_get(row, "elapsed_ms", 0) or 0),
        started_at=started_at,
        won_at=_row_get(row, "won_at"),
        finished_at=_row_get(row, "finished_at"),
        recordable=bool(_row_get(row, "recordable", True)),
    )


async def get_active_daily_game_mode() -> str:
    value = (await get_global_setting(DAILY_GAME_MODE_SETTING_KEY, DAILY_GAME_MODE_CROCODILE)).strip().lower()
    if value in {"2048", "daily2048", "daily_2048"}:
        return DAILY_GAME_MODE_2048
    return DAILY_GAME_MODE_CROCODILE


def _default_spawn_sequence(puzzle_date: date, *, attempt: int = 0, count: int = 220) -> list[dict[str, int]]:
    rng = random.Random(f"daily2048:{puzzle_date.isoformat()}:spawn:{attempt}")
    sequence: list[dict[str, int]] = []
    for _ in range(count):
        sequence.append(
            {
                "x": rng.randrange(DEFAULT_BOARD_SIZE),
                "y": rng.randrange(DEFAULT_BOARD_SIZE),
                "value": 4 if rng.random() < 0.12 else 2,
            }
        )
    return sequence


def _board_from_values(rng: random.Random, values: list[int]) -> list[list[int]]:
    board = [[0 for _ in range(DEFAULT_BOARD_SIZE)] for _ in range(DEFAULT_BOARD_SIZE)]
    path = list(_SNAKE_PATHS[rng.randrange(len(_SNAKE_PATHS))])
    if rng.random() < 0.45:
        path = list(reversed(path))

    used: set[tuple[int, int]] = set()
    for index, value in enumerate(values):
        window = [cell for cell in path[index : index + 4] if cell not in used]
        if not window:
            window = [cell for cell in path if cell not in used]
        if not window:
            break
        x, y = rng.choice(window)
        used.add((x, y))
        board[y][x] = value
    return board


def _build_default_candidate(puzzle_date: date, attempt: int) -> Daily2048Puzzle:
    ordinal = puzzle_date.toordinal()
    goal_type, goal_value, par_moves, values = _GOAL_PATTERNS[(ordinal + attempt * 5) % len(_GOAL_PATTERNS)]
    rng = random.Random(f"daily2048:{puzzle_date.isoformat()}:board:{attempt}")
    values = list(values)
    low_tail = values[3:]
    rng.shuffle(low_tail)
    values = values[:3] + low_tail
    board = _board_from_values(rng, values)
    seed = hashlib.sha256(f"daily2048:{puzzle_date.isoformat()}:{attempt}".encode()).hexdigest()[:16]
    return Daily2048Puzzle(
        puzzle_date=puzzle_date,
        board=board,
        goal_type=goal_type,
        goal_value=goal_value,
        spawn_sequence=_default_spawn_sequence(puzzle_date, attempt=attempt),
        seed=seed,
        par_moves=par_moves,
        target_seconds=DEFAULT_TARGET_SECONDS,
        status="ready",
    )


def build_default_puzzle(
    puzzle_date: date,
    *,
    used_signatures: set[str] | None = None,
    used_board_signatures: set[str] | None = None,
) -> Daily2048Puzzle:
    used_signatures = used_signatures or set()
    used_board_signatures = used_board_signatures or set()
    fallback = _build_default_candidate(puzzle_date, 0)
    for attempt in range(128):
        candidate = _build_default_candidate(puzzle_date, attempt)
        if puzzle_signature(candidate) in used_signatures:
            continue
        if board_signature(candidate.board) in used_board_signatures:
            continue
        return candidate
    return fallback


async def get_puzzle(puzzle_date: date) -> Daily2048Puzzle | None:
    rows = await db.db_query(
        """
        SELECT puzzle_date, board, goal_type, goal_value, spawn_sequence, seed,
               par_moves, target_seconds, status, solution_moves, prepared_at
        FROM public.daily_2048_puzzles
        WHERE puzzle_date = $1
        """,
        (puzzle_date,),
    )
    return _row_to_puzzle(rows[0]) if rows else None


async def get_existing_puzzle_signatures(*, limit: int = DEFAULT_SIGNATURE_LOOKBACK) -> set[str]:
    rows = await db.db_query(
        """
        SELECT puzzle_date, board, goal_type, goal_value, spawn_sequence, seed,
               par_moves, target_seconds, status, solution_moves, prepared_at
        FROM public.daily_2048_puzzles
        WHERE status <> 'disabled'
        ORDER BY puzzle_date DESC
        LIMIT $1
        """,
        (max(1, int(limit)),),
    )
    return {puzzle_signature(_row_to_puzzle(row)) for row in rows}


async def get_existing_board_signatures(*, limit: int = DEFAULT_SIGNATURE_LOOKBACK) -> set[str]:
    rows = await db.db_query(
        """
        SELECT board
        FROM public.daily_2048_puzzles
        WHERE status <> 'disabled'
        ORDER BY puzzle_date DESC
        LIMIT $1
        """,
        (max(1, int(limit)),),
    )
    return {board_signature(_row_get(row, "board")) for row in rows}


async def upsert_puzzle(
    puzzle_date: date,
    *,
    board: list[list[int]],
    goal_type: str,
    goal_value: int,
    spawn_sequence: list[dict[str, int]],
    seed: str,
    par_moves: int,
    target_seconds: int,
    status: str,
    solution_moves: str = "",
) -> Daily2048Puzzle:
    normalized_board = normalize_board(board)
    normalized_goal = normalize_goal_type(goal_type)
    normalized_sequence = normalize_spawn_sequence(spawn_sequence)
    normalized_status = status if status in {"draft", "ready", "disabled"} else "draft"
    rows = await db.db_query(
        """
        INSERT INTO public.daily_2048_puzzles (
            puzzle_date, board, goal_type, goal_value, spawn_sequence, seed,
            par_moves, target_seconds, status, solution_moves, prepared_at
        )
        VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6, $7, $8, $9, $10,
                CASE WHEN $9 = 'ready' THEN NOW() ELSE NULL END)
        ON CONFLICT (puzzle_date) DO UPDATE SET
            board = EXCLUDED.board,
            goal_type = EXCLUDED.goal_type,
            goal_value = EXCLUDED.goal_value,
            spawn_sequence = EXCLUDED.spawn_sequence,
            seed = EXCLUDED.seed,
            par_moves = EXCLUDED.par_moves,
            target_seconds = EXCLUDED.target_seconds,
            status = EXCLUDED.status,
            solution_moves = EXCLUDED.solution_moves,
            prepared_at = CASE WHEN EXCLUDED.status = 'ready' THEN COALESCE(public.daily_2048_puzzles.prepared_at, NOW()) ELSE NULL END,
            updated_at = NOW()
        RETURNING puzzle_date, board, goal_type, goal_value, spawn_sequence, seed,
                  par_moves, target_seconds, status, solution_moves, prepared_at
        """,
        (
            puzzle_date,
            normalized_board,
            normalized_goal,
            max(8, int(goal_value)),
            normalized_sequence,
            seed,
            max(1, int(par_moves)),
            max(30, int(target_seconds)),
            normalized_status,
            solution_moves,
        ),
    )
    return _row_to_puzzle(rows[0])


async def ensure_puzzle(puzzle_date: date) -> Daily2048Puzzle:
    existing = await get_puzzle(puzzle_date)
    if existing:
        return existing
    used_signatures = await get_existing_puzzle_signatures()
    used_board_signatures = await get_existing_board_signatures()
    default = build_default_puzzle(
        puzzle_date,
        used_signatures=used_signatures,
        used_board_signatures=used_board_signatures,
    )
    return await upsert_puzzle(
        puzzle_date,
        board=default.board,
        goal_type=default.goal_type,
        goal_value=default.goal_value,
        spawn_sequence=default.spawn_sequence,
        seed=default.seed,
        par_moves=default.par_moves,
        target_seconds=default.target_seconds,
        status=default.status,
        solution_moves=default.solution_moves,
    )


async def ensure_today_puzzle(now: datetime | None = None) -> Daily2048Puzzle:
    return await ensure_puzzle(today_puzzle_date(now))


async def list_puzzles(*, limit: int = 20) -> list[Daily2048Puzzle]:
    rows = await db.db_query(
        """
        SELECT puzzle_date, board, goal_type, goal_value, spawn_sequence, seed,
               par_moves, target_seconds, status, solution_moves, prepared_at
        FROM public.daily_2048_puzzles
        ORDER BY puzzle_date DESC
        LIMIT $1
        """,
        (limit,),
    )
    return [_row_to_puzzle(row) for row in rows]


async def get_or_create_result(user_id: int, puzzle: Daily2048Puzzle) -> Daily2048Result:
    await crocodile_daily.ensure_user(user_id)
    rows = await db.db_query(
        """
        INSERT INTO public.daily_2048_results (
            user_id, puzzle_date, status, board, spawn_index, moves, merge_score,
            final_score, elapsed_ms, recordable
        )
        VALUES ($1, $2, 'active', $3::jsonb, 0, 0, 0, 0, 0, TRUE)
        ON CONFLICT (user_id, puzzle_date) DO UPDATE
            SET updated_at = public.daily_2048_results.updated_at
        RETURNING user_id, puzzle_date, status, board, spawn_index, moves, merge_score,
                  final_score, elapsed_ms, started_at, won_at, finished_at, recordable
        """,
        (user_id, puzzle.puzzle_date, normalize_board(puzzle.board)),
    )
    return _row_to_result(rows[0])


async def get_result(user_id: int, puzzle_date: date) -> Daily2048Result | None:
    rows = await db.db_query(
        """
        SELECT user_id, puzzle_date, status, board, spawn_index, moves, merge_score,
               final_score, elapsed_ms, started_at, won_at, finished_at, recordable
        FROM public.daily_2048_results
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
    )
    return _row_to_result(rows[0]) if rows else None


async def update_result_after_move(
    *,
    user_id: int,
    puzzle_date: date,
    board: list[list[int]],
    spawn_index: int,
    moves: int,
    merge_score: int,
    elapsed_ms: int,
    status: str,
    final_score: int,
    won: bool,
    finished: bool,
) -> Daily2048Result:
    rows = await db.db_query(
        """
        UPDATE public.daily_2048_results
        SET status = $3,
            board = $4::jsonb,
            spawn_index = $5,
            moves = $6,
            merge_score = $7,
            elapsed_ms = $8,
            final_score = CASE WHEN $10 THEN $9 ELSE final_score END,
            won_at = CASE WHEN $11 THEN NOW() ELSE won_at END,
            finished_at = CASE WHEN $12 THEN NOW() ELSE finished_at END,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2 AND status = 'active'
        RETURNING user_id, puzzle_date, status, board, spawn_index, moves, merge_score,
                  final_score, elapsed_ms, started_at, won_at, finished_at, recordable
        """,
        (
            user_id,
            puzzle_date,
            status,
            normalize_board(board),
            max(0, int(spawn_index)),
            max(0, int(moves)),
            max(0, int(merge_score)),
            max(0, int(elapsed_ms)),
            max(0, int(final_score)),
            won,
            won,
            finished,
        ),
    )
    if rows:
        return _row_to_result(rows[0])
    existing = await get_result(user_id, puzzle_date)
    if existing:
        return existing
    raise RuntimeError(f"daily 2048 result missing for user={user_id} date={puzzle_date}")


async def update_result_elapsed(
    *,
    user_id: int,
    puzzle_date: date,
    elapsed_ms: int,
) -> Daily2048Result | None:
    rows = await db.db_query(
        """
        UPDATE public.daily_2048_results
        SET elapsed_ms = $3,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2 AND status = 'active'
        RETURNING user_id, puzzle_date, status, board, spawn_index, moves, merge_score,
                  final_score, elapsed_ms, started_at, won_at, finished_at, recordable
        """,
        (user_id, puzzle_date, max(0, int(elapsed_ms))),
    )
    return _row_to_result(rows[0]) if rows else None


async def register_prompt_message(
    *,
    user_id: int,
    puzzle_date: date,
    chat_id: int,
    message_id: int,
) -> None:
    await db.db_query(
        """
        INSERT INTO public.daily_2048_prompt_messages (user_id, puzzle_date, chat_id, message_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, puzzle_date) DO UPDATE SET
            chat_id = EXCLUDED.chat_id,
            message_id = EXCLUDED.message_id,
            is_active = TRUE,
            updated_at = NOW()
        """,
        (user_id, puzzle_date, chat_id, message_id),
    )


async def get_active_prompt_message(user_id: int, puzzle_date: date) -> dict[str, Any] | None:
    rows = await db.db_query(
        """
        SELECT id, user_id, puzzle_date, chat_id, message_id
        FROM public.daily_2048_prompt_messages
        WHERE user_id = $1 AND puzzle_date = $2 AND is_active = TRUE
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, puzzle_date),
    )
    return rows[0] if rows else None


async def deactivate_prompt_message(user_id: int, puzzle_date: date) -> None:
    await db.db_query(
        """
        UPDATE public.daily_2048_prompt_messages
        SET is_active = FALSE,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
    )


async def get_leaderboard(puzzle_date: date, *, limit: int = 10) -> list[dict[str, Any]]:
    rows = await db.db_query(
        """
        SELECT r.user_id, r.final_score, r.moves, r.elapsed_ms, r.merge_score, r.won_at,
               u.display_name
        FROM public.daily_2048_results r
        LEFT JOIN public.users u ON u.user_id = r.user_id
        WHERE r.puzzle_date = $1
          AND r.status = 'won'
          AND r.recordable = TRUE
        ORDER BY r.final_score DESC, r.moves ASC, r.elapsed_ms ASC, r.won_at ASC NULLS LAST
        LIMIT $2
        """,
        (puzzle_date, limit),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        uid = int(item.get("user_id") or 0)
        item["display_name"] = (item.get("display_name") or "").strip() or f"игрок {str(uid)[-4:]}"
        out.append(item)
    return out


async def get_rank(user_id: int, puzzle_date: date) -> int | None:
    rows = await db.db_query(
        """
        WITH ranked AS (
            SELECT user_id,
                   ROW_NUMBER() OVER (
                       ORDER BY final_score DESC, moves ASC, elapsed_ms ASC, won_at ASC NULLS LAST
                   ) AS rank
            FROM public.daily_2048_results
            WHERE puzzle_date = $1
              AND status = 'won'
              AND recordable = TRUE
        )
        SELECT rank FROM ranked WHERE user_id = $2
        """,
        (puzzle_date, user_id),
    )
    return int(rows[0]["rank"]) if rows else None


async def get_monthly_champions(year: int, month: int) -> list[dict[str, Any]]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    rows = await db.db_query(
        """
        WITH ranked AS (
            SELECT r.puzzle_date, r.user_id, r.final_score, r.moves, r.elapsed_ms, r.won_at,
                   u.display_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.puzzle_date
                       ORDER BY r.final_score DESC, r.moves ASC, r.elapsed_ms ASC, r.won_at ASC NULLS LAST
                   ) AS day_rank
            FROM public.daily_2048_results r
            LEFT JOIN public.users u ON u.user_id = r.user_id
            WHERE r.puzzle_date >= $1
              AND r.puzzle_date < $2
              AND r.status = 'won'
              AND r.recordable = TRUE
        )
        SELECT puzzle_date, user_id, final_score, moves, elapsed_ms, won_at, display_name
        FROM ranked
        WHERE day_rank = 1
        ORDER BY puzzle_date ASC
        """,
        (start, end),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        uid = int(item.get("user_id") or 0)
        item["display_name"] = (item.get("display_name") or "").strip() or f"игрок {str(uid)[-4:]}"
        out.append(item)
    return out


async def get_delivery_status(puzzle_date: date) -> dict[str, Any]:
    rows = await db.db_query(
        """
        SELECT COUNT(*) FILTER (WHERE status = 'active') AS active,
               COUNT(*) FILTER (WHERE status = 'won') AS won,
               COUNT(*) FILTER (WHERE status = 'lost') AS lost,
               COUNT(*) FILTER (WHERE status IN ('won', 'lost')) AS finished
        FROM public.daily_2048_results
        WHERE puzzle_date = $1
        """,
        (puzzle_date,),
    )
    return dict(rows[0]) if rows else {"active": 0, "won": 0, "lost": 0, "finished": 0}

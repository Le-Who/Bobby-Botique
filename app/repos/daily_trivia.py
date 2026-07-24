from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app import database as db
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

DAILY_TRIVIA_PREP_DAYS_AHEAD: int = 7


@dataclass(frozen=True)
class TriviaQuestion:
    id: int
    topic: str
    question: str
    options: list[str]
    correct_index: int
    explanation: str


@dataclass(frozen=True)
class DailyTriviaPuzzle:
    puzzle_date: date
    questions: list[TriviaQuestion]
    status: str
    prepared_at: datetime | None


@dataclass(frozen=True)
class DailyTriviaResult:
    user_id: int
    puzzle_date: date
    status: str
    current_question: int
    correct_count: int
    final_score: int
    elapsed_ms: int
    answers: list[dict[str, Any]]
    started_at: datetime
    finished_at: datetime | None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def normalize_questions(raw: Any) -> list[TriviaQuestion]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, list):
        return []
    result = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        opts = item.get("options", [])
        if not isinstance(opts, list):
            opts = [str(opts)]
        opts_clean = [str(o) for o in opts]
        correct_idx = int(item.get("correct_index", 0))
        if correct_idx < 0 or correct_idx >= len(opts_clean):
            correct_idx = 0
        result.append(
            TriviaQuestion(
                id=int(item.get("id", idx + 1)),
                topic=str(item.get("topic", "Общие знания")),
                question=str(item.get("question", "")),
                options=opts_clean,
                correct_index=correct_idx,
                explanation=str(item.get("explanation", "")),
            )
        )
    return result


def questions_to_dict_list(questions: list[TriviaQuestion]) -> list[dict[str, Any]]:
    return [
        {
            "id": q.id,
            "topic": q.topic,
            "question": q.question,
            "options": q.options,
            "correct_index": q.correct_index,
            "explanation": q.explanation,
        }
        for q in questions
    ]


def _row_to_puzzle(row: Any) -> DailyTriviaPuzzle:
    return DailyTriviaPuzzle(
        puzzle_date=_row_get(row, "puzzle_date"),
        questions=normalize_questions(_row_get(row, "questions")),
        status=str(_row_get(row, "status", "ready") or "ready"),
        prepared_at=_row_get(row, "prepared_at"),
    )


def _row_to_result(row: Any) -> DailyTriviaResult:
    started_at = _row_get(row, "started_at") or datetime.now(tz=UTC)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    answers_raw = _row_get(row, "answers", [])
    if isinstance(answers_raw, str):
        try:
            answers_raw = json.loads(answers_raw)
        except Exception:
            answers_raw = []
    return DailyTriviaResult(
        user_id=int(_row_get(row, "user_id") or 0),
        puzzle_date=_row_get(row, "puzzle_date"),
        status=str(_row_get(row, "status", "active") or "active"),
        current_question=int(_row_get(row, "current_question", 0) or 0),
        correct_count=int(_row_get(row, "correct_count", 0) or 0),
        final_score=int(_row_get(row, "final_score", 0) or 0),
        elapsed_ms=int(_row_get(row, "elapsed_ms", 0) or 0),
        answers=answers_raw if isinstance(answers_raw, list) else [],
        started_at=started_at,
        finished_at=_row_get(row, "finished_at"),
    )


async def get_puzzle(puzzle_date: date, *, conn=None) -> DailyTriviaPuzzle | None:
    rows = await db.db_query(
        """
        SELECT puzzle_date, questions, status, prepared_at
        FROM public.daily_trivia_puzzles
        WHERE puzzle_date = $1
        """,
        (puzzle_date,),
        conn=conn,
    )
    if not rows:
        return None
    return _row_to_puzzle(rows[0])


async def save_puzzle(
    puzzle_date: date,
    questions: list[TriviaQuestion],
    status: str = "ready",
    *,
    conn=None,
) -> DailyTriviaPuzzle:
    q_dict = questions_to_dict_list(questions)
    q_json = json.dumps(q_dict)
    now = datetime.now(tz=UTC)
    rows = await db.db_query(
        """
        INSERT INTO public.daily_trivia_puzzles (puzzle_date, questions, status, prepared_at)
        VALUES ($1, $2::jsonb, $3, $4)
        ON CONFLICT (puzzle_date) DO UPDATE
        SET questions = EXCLUDED.questions,
            status = EXCLUDED.status,
            prepared_at = EXCLUDED.prepared_at,
            updated_at = NOW()
        RETURNING puzzle_date, questions, status, prepared_at
        """,
        (puzzle_date, q_json, status, now if status == "ready" else None),
        conn=conn,
    )
    return _row_to_puzzle(rows[0])


async def get_result_if_exists(user_id: int, puzzle_date: date, *, conn=None) -> DailyTriviaResult | None:
    """Return the user's trivia result for today if it exists, else None.

    Unlike get_or_create_result this never inserts a row — safe to call on
    every page load without polluting the results table.
    """
    rows = await db.db_query(
        """
        SELECT user_id, puzzle_date, status, current_question, correct_count,
               final_score, elapsed_ms, answers, started_at, finished_at
        FROM public.daily_trivia_results
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
        conn=conn,
    )
    return _row_to_result(rows[0]) if rows else None


async def get_or_create_result(user_id: int, puzzle_date: date, *, conn=None) -> DailyTriviaResult:
    rows = await db.db_query(
        """
        SELECT user_id, puzzle_date, status, current_question, correct_count,
               final_score, elapsed_ms, answers, started_at, finished_at
        FROM public.daily_trivia_results
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
        conn=conn,
    )
    if rows:
        return _row_to_result(rows[0])

    rows = await db.db_query(
        """
        INSERT INTO public.daily_trivia_results (user_id, puzzle_date)
        VALUES ($1, $2)
        ON CONFLICT (user_id, puzzle_date) DO NOTHING
        RETURNING user_id, puzzle_date, status, current_question, correct_count,
                  final_score, elapsed_ms, answers, started_at, finished_at
        """,
        (user_id, puzzle_date),
        conn=conn,
    )
    if rows:
        return _row_to_result(rows[0])

    res = await db.db_query(
        """
        SELECT user_id, puzzle_date, status, current_question, correct_count,
               final_score, elapsed_ms, answers, started_at, finished_at
        FROM public.daily_trivia_results
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
        conn=conn,
    )
    return _row_to_result(res[0])


async def update_result_answer(
    user_id: int,
    puzzle_date: date,
    *,
    current_question: int,
    correct_count: int,
    final_score: int,
    elapsed_ms: int,
    answers: list[dict[str, Any]],
    status: str = "active",
    finished: bool = False,
) -> DailyTriviaResult:
    answers_json = json.dumps(answers)
    rows = await db.db_query(
        """
        UPDATE public.daily_trivia_results
        SET current_question = $3,
            correct_count = $4,
            final_score = $5,
            elapsed_ms = $6,
            answers = $7::jsonb,
            status = $8,
            finished_at = CASE WHEN $9::boolean THEN NOW() ELSE finished_at END,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2
        RETURNING user_id, puzzle_date, status, current_question, correct_count,
                  final_score, elapsed_ms, answers, started_at, finished_at
        """,
        (
            user_id,
            puzzle_date,
            current_question,
            correct_count,
            final_score,
            elapsed_ms,
            answers_json,
            status,
            finished,
        ),
    )
    return _row_to_result(rows[0])


async def get_question_by_id(question_id: int, *, conn=None) -> TriviaQuestion | None:
    """Find a single trivia question by its id across all stored puzzles.

    Questions are stored as a JSONB array inside daily_trivia_puzzles.
    We use jsonb_array_elements to search across all puzzles without needing
    a separate table.  Returns None if not found.
    """
    rows = await db.db_query(
        """
        SELECT elem
        FROM public.daily_trivia_puzzles,
             jsonb_array_elements(questions) AS elem
        WHERE (elem->>'id')::int = $1
        LIMIT 1
        """,
        (question_id,),
        conn=conn,
    )
    if not rows:
        return None
    item = rows[0]["elem"]
    if isinstance(item, str):
        import json as _json
        try:
            item = _json.loads(item)
        except Exception:
            return None
    opts = item.get("options", [])
    if not isinstance(opts, list):
        opts = [str(opts)]
    opts_clean = [str(o) for o in opts]
    correct_idx = int(item.get("correct_index", 0))
    if correct_idx < 0 or correct_idx >= len(opts_clean):
        correct_idx = 0
    return TriviaQuestion(
        id=int(item.get("id", question_id)),
        topic=str(item.get("topic", "Общие знания")),
        question=str(item.get("question", "")),
        options=opts_clean,
        correct_index=correct_idx,
        explanation=str(item.get("explanation", "")),
    )


async def get_puzzles_range(start_date: date, end_date: date, *, conn=None) -> list[DailyTriviaPuzzle]:
    rows = await db.db_query(
        """
        SELECT puzzle_date, questions, status, prepared_at
        FROM public.daily_trivia_puzzles
        WHERE puzzle_date >= $1 AND puzzle_date <= $2
        ORDER BY puzzle_date DESC
        """,
        (start_date, end_date),
        conn=conn,
    )
    return [_row_to_puzzle(r) for r in rows]


async def get_delivery_status(puzzle_date: date, *, conn=None) -> dict[str, Any]:
    rows = await db.db_query(
        """
        SELECT
            COUNT(*) FILTER (WHERE is_subscribed = TRUE) AS total_subscribed,
            COUNT(*) FILTER (WHERE is_subscribed = TRUE AND last_sent_puzzle_date = $1) AS sent_today,
            COUNT(*) FILTER (WHERE is_subscribed = TRUE AND (last_sent_puzzle_date IS NULL OR last_sent_puzzle_date < $1)) AS pending_today
        FROM public.daily_trivia_preferences
        """,
        (puzzle_date,),
        conn=conn,
    )
    if not rows:
        return {"total_subscribed": 0, "sent_today": 0, "pending_today": 0}
    r = rows[0]
    return {
        "total_subscribed": int(_row_get(r, "total_subscribed", 0) or 0),
        "sent_today": int(_row_get(r, "sent_today", 0) or 0),
        "pending_today": int(_row_get(r, "pending_today", 0) or 0),
    }


async def set_user_subscription(user_id: int, is_subscribed: bool, *, conn=None) -> None:
    await db.db_query(
        """
        INSERT INTO public.daily_trivia_preferences (user_id, is_subscribed)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET is_subscribed = EXCLUDED.is_subscribed, updated_at = NOW()
        """,
        (user_id, is_subscribed),
        conn=conn,
    )


async def get_daily_leaderboard(puzzle_date: date, limit: int = 10, *, conn=None) -> list[dict[str, Any]]:
    rows = await db.db_query(
        """
        SELECT r.user_id, r.final_score, r.correct_count, r.elapsed_ms, u.display_name
        FROM public.daily_trivia_results r
        LEFT JOIN public.users u ON r.user_id = u.user_id
        WHERE r.puzzle_date = $1 AND r.status = 'completed'
        ORDER BY r.final_score DESC, r.elapsed_ms ASC
        LIMIT $2
        """,
        (puzzle_date, limit),
        conn=conn,
    )
    return [
        {
            "user_id": _row_get(r, "user_id"),
            "name": (_row_get(r, "display_name") or "").strip() or f"User {str(_row_get(r, 'user_id'))[-4:]}",
            "score": int(_row_get(r, "final_score", 0)),
            "correct": int(_row_get(r, "correct_count", 0)),
            "elapsed_ms": int(_row_get(r, "elapsed_ms", 0) or 0),
        }
        for r in rows
    ]


async def get_monthly_leaderboard(year: int, month: int, limit: int = 10, *, conn=None) -> list[dict[str, Any]]:
    rows = await db.db_query(
        """
        SELECT r.user_id, SUM(r.final_score) as total_score, SUM(r.correct_count) as total_correct, COUNT(r.puzzle_date) as games_played, u.display_name
        FROM public.daily_trivia_results r
        LEFT JOIN public.users u ON r.user_id = u.user_id
        WHERE EXTRACT(YEAR FROM r.puzzle_date) = $1 AND EXTRACT(MONTH FROM r.puzzle_date) = $2 AND r.status = 'completed'
        GROUP BY r.user_id, u.display_name
        ORDER BY total_score DESC, games_played DESC
        LIMIT $3
        """,
        (year, month, limit),
        conn=conn,
    )
    return [
        {
            "user_id": _row_get(r, "user_id"),
            "name": (_row_get(r, "display_name") or "").strip() or f"User {str(_row_get(r, 'user_id'))[-4:]}",
            "score": int(_row_get(r, "total_score", 0)),
            "correct": int(_row_get(r, "total_correct", 0)),
            "games_played": int(_row_get(r, "games_played", 0)),
        }
        for r in rows
    ]


async def get_admin_stats(today: date, *, conn=None) -> dict[str, Any]:
    delivery = await get_delivery_status(today, conn=conn)
    total_subbed = delivery["total_subscribed"]

    played_rows = await db.db_query(
        """
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'completed') as finished,
            COUNT(*) FILTER (WHERE status = 'active') as active
        FROM public.daily_trivia_results
        WHERE puzzle_date = $1
        """,
        (today,),
        conn=conn,
    )
    total_played = 0
    finished_count = 0
    active_count = 0
    if played_rows:
        total_played = int(_row_get(played_rows[0], "total", 0) or 0)
        finished_count = int(_row_get(played_rows[0], "finished", 0) or 0)
        active_count = int(_row_get(played_rows[0], "active", 0) or 0)

    puzzle_rows = await db.db_query(
        """
        SELECT COUNT(*) as cnt
        FROM public.daily_trivia_puzzles
        WHERE puzzle_date >= $1 AND status = 'ready'
        """,
        (today,),
        conn=conn,
    )
    ready_puzzles = int(_row_get(puzzle_rows[0], "cnt", 0) if puzzle_rows else 0)

    return {
        "total_subscribed": total_subbed,
        "played_today": total_played,
        "finished_today": finished_count,
        "active_today": active_count,
        "ready_puzzles_ahead": ready_puzzles,
    }


def _is_fuzzy_similar(str1: str, str2: str, threshold: float = 0.85) -> bool:
    if str1 == str2:
        return True
    try:
        from rapidfuzz import fuzz
        return (fuzz.token_sort_ratio(str1, str2) / 100.0) >= threshold
    except ImportError:
        import difflib
        return difflib.SequenceMatcher(None, str1, str2).ratio() >= threshold


async def get_used_keys(days: int = 90, *, conn=None) -> list[dict[str, str]]:
    rows = await db.db_query(
        """
        SELECT object_norm AS object, subobject_norm AS subobject
        FROM public.daily_trivia_used_keys
        WHERE used_at >= CURRENT_DATE - MAKE_INTERVAL(days => $1::int)
        ORDER BY used_at DESC
        """,
        (days,),
        conn=conn,
    )
    return [
        {
            "object": str(_row_get(r, "object", "") or ""),
            "subobject": str(_row_get(r, "subobject", "") or ""),
        }
        for r in rows
    ]


async def save_used_keys(keys: list[dict[str, str]], p_date: date | None = None, *, conn=None) -> None:
    if not keys:
        return

    target_date = p_date or date.today()
    existing = await get_used_keys(days=90, conn=conn)

    to_insert: list[tuple[str, str]] = []
    for k in keys:
        obj = str(k.get("object", "")).strip().lower()
        subobj = str(k.get("subobject", "")).strip().lower()
        if not obj or not subobj:
            continue

        is_dup = False
        for ex in existing:
            ex_obj = ex["object"].strip().lower()
            ex_sub = ex["subobject"].strip().lower()
            if obj == ex_obj and _is_fuzzy_similar(subobj, ex_sub, threshold=0.85):
                is_dup = True
                break

        if not is_dup:
            for ins_obj, ins_sub in to_insert:
                if obj == ins_obj and _is_fuzzy_similar(subobj, ins_sub, threshold=0.85):
                    is_dup = True
                    break

        if not is_dup:
            to_insert.append((obj, subobj))

    for obj_norm, subobj_norm in to_insert:
        await db.db_query(
            """
            INSERT INTO public.daily_trivia_used_keys (object_norm, subobject_norm, used_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (object_norm, subobject_norm) DO NOTHING
            """,
            (obj_norm, subobj_norm, target_date),
            conn=conn,
        )


async def cleanup_old_used_keys(days: int = 90, *, conn=None) -> int:
    """Delete the single day that just rolled out of the sliding window.

    On day N (N > `days`), deletes entries from exactly `days` days ago.
    This ensures only one day's worth of entries is removed per scheduled run,
    implementing a true sliding-window TTL rather than a bulk expiry.
    """
    result = await db.db_query(
        """
        DELETE FROM public.daily_trivia_used_keys
        WHERE used_at = CURRENT_DATE - MAKE_INTERVAL(days => $1::int)
        """,
        (days,),
        conn=conn,
    )
    return len(result) if result else 0


# ---------------------------------------------------------------------------
# Prompt-message tracking (for in-place result editing)
# ---------------------------------------------------------------------------


async def register_prompt_message(
    *,
    user_id: int,
    puzzle_date: date,
    chat_id: int,
    message_id: int,
) -> None:
    """Record the Telegram message that was sent as the daily trivia invite.

    Stored so we can edit it in-place when the player finishes the quiz.
    Uses UPSERT so that re-sends (e.g. /trivia command) replace the old record.
    """
    await db.db_query(
        """
        INSERT INTO public.daily_trivia_prompt_messages (user_id, puzzle_date, chat_id, message_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, puzzle_date) DO UPDATE SET
            chat_id    = EXCLUDED.chat_id,
            message_id = EXCLUDED.message_id,
            is_active  = TRUE,
            updated_at = NOW()
        """,
        (user_id, puzzle_date, chat_id, message_id),
    )


async def get_active_prompt_message(user_id: int, puzzle_date: date) -> dict[str, Any] | None:
    """Return the active prompt message for the given user/date, or None."""
    rows = await db.db_query(
        """
        SELECT id, user_id, puzzle_date, chat_id, message_id
        FROM public.daily_trivia_prompt_messages
        WHERE user_id = $1 AND puzzle_date = $2 AND is_active = TRUE
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, puzzle_date),
    )
    return rows[0] if rows else None


async def deactivate_prompt_message(user_id: int, puzzle_date: date) -> None:
    """Mark the prompt message as inactive (edit already done or failed)."""
    await db.db_query(
        """
        UPDATE public.daily_trivia_prompt_messages
        SET is_active  = FALSE,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
    )




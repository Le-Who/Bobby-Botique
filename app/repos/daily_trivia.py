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

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import database as db
from app.config import KYIV_TZ
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Kyiv"
DISCOVERY_SNOOZE_DAYS = 14
DAILY_MAX_ATTEMPTS = 6
DAILY_PREP_DAYS_AHEAD = 7
DAILY_IMAGE_PREP_DAYS_AHEAD = 2
DAILY_IMAGE_MODEL = "qwen-image"
DAILY_DELIVERY_SETTING_KEY = "daily_crocodile_delivery_enabled"
DAILY_DIFFICULTIES = ("easy", "hard")
DAILY_DIFFICULTY_LABELS = {
    "easy": "Easy",
    "hard": "Hard",
}


@dataclass(slots=True)
class DailyPuzzle:
    puzzle_date: date
    target_word: str
    topic: str
    lang: str
    difficulty: str = "easy"
    hints: list[str] = field(default_factory=list)
    image_prompt: str = ""
    image_file_id: str = ""
    image_model: str = DAILY_IMAGE_MODEL
    prepared_at: datetime | None = None


@dataclass(slots=True)
class DailyResult:
    user_id: int
    puzzle_date: date
    status: str
    attempts: list[dict[str, Any]]
    best_score: float
    used_hints_count: int
    won_at: datetime | None
    finished_at: datetime | None
    points: int
    share_grid: str
    streak_after: int
    difficulty: str = "easy"


def today_puzzle_date(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=UTC)
    return current.astimezone(KYIV_TZ).date()


def _safe_zoneinfo(timezone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def normalize_timezone(timezone: str | None) -> str:
    tz = (timezone or "").strip()
    if not tz:
        return DEFAULT_TIMEZONE
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE
    return tz


def local_now(timezone: str | None, now: datetime | None = None) -> datetime:
    current = now or datetime.now(tz=UTC)
    return current.astimezone(_safe_zoneinfo(timezone))


def local_date_for_timezone(timezone: str | None, now: datetime | None = None) -> date:
    return local_now(timezone, now).date()


def was_daily_delivered_today(
    preference: dict[str, Any] | None,
    *,
    puzzle_date: date,
    now: datetime | None = None,
) -> bool:
    if not preference:
        return False
    local_today = local_date_for_timezone(preference.get("timezone"), now)
    last_local = preference.get("last_sent_local_date")
    last_puzzle = preference.get("last_sent_puzzle_date")
    return bool((last_local and last_local >= local_today) or (last_puzzle and last_puzzle >= puzzle_date))


def normalize_daily_difficulty(value: str | None) -> str:
    difficulty = (value or "easy").strip().lower()
    return difficulty if difficulty in DAILY_DIFFICULTIES else "easy"


def daily_difficulty_label(value: str | None) -> str:
    return DAILY_DIFFICULTY_LABELS.get(normalize_daily_difficulty(value), "Easy")


def compute_points(*, won: bool, attempt_count: int, used_hints_count: int) -> int:
    if not won:
        return 0
    return max(200, 1000 - max(0, attempt_count - 1) * 120 - max(0, used_hints_count) * 100)


def build_share_grid(attempts: list[dict[str, Any]], *, won: bool) -> str:
    icons = {
        "cold": "🟥",
        "warm": "🟨",
        "hot": "🟧",
        "exact_match": "🟩",
    }
    grid = "".join(icons.get(str(item.get("status", "")), "⬛") for item in attempts)
    return grid or ("🟩" if won else "⬛")


def render_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _row_to_puzzle(row: dict[str, Any]) -> DailyPuzzle:
    return DailyPuzzle(
        puzzle_date=row["puzzle_date"],
        difficulty=normalize_daily_difficulty(row.get("difficulty")),
        target_word=row["target_word"],
        topic=row["topic"],
        lang=row["lang"],
        hints=[str(item) for item in _json_list(row.get("hints"))],
        image_prompt=str(row.get("image_prompt") or ""),
        image_file_id=str(row.get("image_file_id") or ""),
        image_model=str(row.get("image_model") or DAILY_IMAGE_MODEL),
        prepared_at=row.get("prepared_at"),
    )


def _row_to_result(row: dict[str, Any]) -> DailyResult:
    return DailyResult(
        user_id=int(row["user_id"]),
        puzzle_date=row["puzzle_date"],
        difficulty=normalize_daily_difficulty(row.get("difficulty")),
        status=str(row["status"]),
        attempts=[item for item in _json_list(row.get("attempts")) if isinstance(item, dict)],
        best_score=float(row.get("best_score") or 0),
        used_hints_count=int(row.get("used_hints_count") or 0),
        won_at=row.get("won_at"),
        finished_at=row.get("finished_at"),
        points=int(row.get("points") or 0),
        share_grid=str(row.get("share_grid") or ""),
        streak_after=int(row.get("streak_after") or 0),
    )


async def ensure_user(user_id: int) -> None:
    await db.db_query(
        "INSERT INTO public.users (user_id, is_authorized) VALUES ($1, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )


async def update_user_display_name(user_id: int, display_name: str) -> None:
    """Upsert a human-readable display name for the user.

    Called lazily from the WebApp WebSocket handshake so the leaderboard
    can show real names instead of numeric IDs.
    The column is added by migration 044; the call is a no-op when the
    name hasn't changed or when the column doesn't exist yet.
    """
    name = (display_name or "").strip()[:128]  # guard against absurdly long names
    if not name:
        return
    try:
        await db.db_query(
            """
            INSERT INTO public.users (user_id, is_authorized, display_name)
            VALUES ($1, 0, $2)
            ON CONFLICT (user_id) DO UPDATE
                SET display_name = EXCLUDED.display_name
            """,
            (user_id, name),
        )
    except Exception as exc:  # column may not exist in older schema
        logger.debug("update_user_display_name failed user=%s: %s", user_id, exc)


async def record_player_activity(user_id: int, *, event: str) -> None:
    await ensure_user(user_id)
    started_inc = 1 if event == "classic_started" else 0
    classic_inc = 1 if event == "classic_played" else 0
    daily_inc = 1 if event == "daily_played" else 0
    await db.db_query(
        """
        INSERT INTO public.crocodile_player_activity (
            user_id, classic_games_started, classic_games_played, daily_games_played
        )
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE SET
            last_seen_at = NOW(),
            classic_games_started = public.crocodile_player_activity.classic_games_started + EXCLUDED.classic_games_started,
            classic_games_played = public.crocodile_player_activity.classic_games_played + EXCLUDED.classic_games_played,
            daily_games_played = public.crocodile_player_activity.daily_games_played + EXCLUDED.daily_games_played
        """,
        (user_id, started_inc, classic_inc, daily_inc),
    )


async def ensure_puzzle_day(puzzle_date: date, *, conn=None) -> None:
    await db.db_query(
        """
        INSERT INTO public.crocodile_daily_days (puzzle_date)
        VALUES ($1)
        ON CONFLICT (puzzle_date) DO NOTHING
        """,
        (puzzle_date,),
        conn=conn,
    )


async def get_puzzle(puzzle_date: date, *, difficulty: str = "easy", conn=None) -> DailyPuzzle | None:
    difficulty = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        SELECT puzzle_date, difficulty, target_word, topic, lang, hints,
               image_prompt, image_file_id, image_model, prepared_at
        FROM public.crocodile_daily_puzzles
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty),
        conn=conn,
    )
    return _row_to_puzzle(rows[0]) if rows else None


async def get_puzzles_for_date(puzzle_date: date, *, conn=None) -> dict[str, DailyPuzzle]:
    rows = await db.db_query(
        """
        SELECT puzzle_date, difficulty, target_word, topic, lang, hints,
               image_prompt, image_file_id, image_model, prepared_at
        FROM public.crocodile_daily_puzzles
        WHERE puzzle_date = $1
        ORDER BY difficulty ASC
        """,
        (puzzle_date,),
        conn=conn,
    )
    return {normalize_daily_difficulty(row.get("difficulty")): _row_to_puzzle(row) for row in rows}


def normalize_daily_word(word: str | None) -> str:
    return " ".join((word or "").strip().lower().split())


def is_puzzle_fully_prepared(puzzle: DailyPuzzle) -> bool:
    # Image is generated best-effort and retried hourly via the scheduler.
    # A puzzle is considered ready as soon as hints are available so that
    # deliveries are never blocked by a transient image-generation failure.
    return bool(puzzle.hints)


async def get_used_daily_words(*, days_back: int = 365, conn=None) -> set[str]:
    """Return words used in daily puzzles within the last *days_back* days.

    Using a rolling window instead of querying all-time history lets words
    re-enter the rotation after the cooldown, and prevents cross-difficulty
    repetition within the window (both easy and hard words are included).
    """
    rows = await db.db_query(
        """
        SELECT target_word
        FROM public.crocodile_daily_puzzles
        WHERE target_word <> ''
          AND puzzle_date >= (CURRENT_DATE - $1::int)
        """,
        (days_back,),
        conn=conn,
    )
    return {
        normalize_daily_word(row.get("target_word")) for row in rows if normalize_daily_word(row.get("target_word"))
    }


async def _create_puzzle_if_missing_with_conn(
    puzzle_date: date,
    *,
    difficulty: str = "easy",
    conn=None,
) -> DailyPuzzle:
    difficulty = normalize_daily_difficulty(difficulty)
    existing = await get_puzzle(puzzle_date, difficulty=difficulty, conn=conn)
    if existing:
        return existing

    from app.games.word_bank import (
        WORD_BANK,
        _filter_words_by_difficulty,  # type: ignore[attr-defined]
        pick_random_word_for_topic,
        resolve_topic,
    )

    await ensure_puzzle_day(puzzle_date, conn=conn)

    used_words = await get_used_daily_words(conn=conn)
    if difficulty == "hard":
        easy_puzzle = await get_puzzle(puzzle_date, difficulty="easy", conn=conn)
        if easy_puzzle:
            used_words.add(normalize_daily_word(easy_puzzle.target_word))

    # ── Date-based topic rotation across all RU categories ─────────────────
    # Each day maps to a different category so the user never sees the
    # same topic two days in a row, and topic diversity is maximised.
    ru_categories = list(WORD_BANK.get("ru", {}).keys())  # stable insertion order
    n = len(ru_categories)

    # Hard offset so easy/hard don't always pick the same topic on the same day
    difficulty_offset = 0 if difficulty == "easy" else n // 2

    # Ordered list of categories to try — starting from today's slot, wrapping
    day_ordinal = puzzle_date.toordinal()
    topic_candidates = [ru_categories[(day_ordinal + difficulty_offset + i) % n] for i in range(n)]

    # Try each candidate in rotating order; pick the first that still has
    # unused words of the right difficulty band.
    chosen_topic_raw = topic_candidates[0]  # default: today's slot
    for candidate_cat in topic_candidates:
        candidate_words = list(WORD_BANK["ru"].get(candidate_cat, []))
        available = _filter_words_by_difficulty(
            candidate_words,
            topic_id=f"builtin:ru:{candidate_cat.lower()}",
            preferred_difficulty=difficulty,
        )
        unused = [w for w in available if normalize_daily_word(w) not in used_words]
        if unused:
            chosen_topic_raw = candidate_cat
            break
    # If ALL categories are exhausted just use today's slot — the AI
    # word-generation fallback inside pick_random_word_for_topic will kick in.

    topic = resolve_topic(chosen_topic_raw)

    word, lang, category, _ = await pick_random_word_for_topic(
        topic,
        used_words=used_words,
        preferred_difficulty=difficulty,
    )
    rows = await db.db_query(
        """
        INSERT INTO public.crocodile_daily_puzzles (
            puzzle_date, difficulty, target_word, topic, lang, image_model
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (puzzle_date, difficulty) DO NOTHING
        RETURNING puzzle_date, difficulty, target_word, topic, lang, hints,
                  image_prompt, image_file_id, image_model, prepared_at
        """,
        (puzzle_date, difficulty, word, category, lang, DAILY_IMAGE_MODEL),
        conn=conn,
    )
    if rows:
        return _row_to_puzzle(rows[0])

    # Another worker won the race.
    puzzle = await get_puzzle(puzzle_date, difficulty=difficulty, conn=conn)
    if not puzzle:
        raise RuntimeError(f"daily puzzle was not created for {puzzle_date} difficulty={difficulty}")
    return puzzle


async def create_puzzle_if_missing(puzzle_date: date, *, difficulty: str = "easy") -> DailyPuzzle:
    pool = getattr(db.db_manager, "pool", None)
    if not pool or getattr(pool, "_closed", False):
        return await _create_puzzle_if_missing_with_conn(puzzle_date, difficulty=difficulty)

    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("LOCK TABLE public.crocodile_daily_days IN SHARE ROW EXCLUSIVE MODE")
        await conn.execute("LOCK TABLE public.crocodile_daily_puzzles IN SHARE ROW EXCLUSIVE MODE")
        return await _create_puzzle_if_missing_with_conn(puzzle_date, difficulty=difficulty, conn=conn)


async def set_puzzle_hints(puzzle_date: date, hints: list[str], *, difficulty: str = "easy") -> None:
    difficulty = normalize_daily_difficulty(difficulty)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_puzzles
        SET hints = $3::jsonb,
            prepared_at = NULL
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty, json.dumps(hints, ensure_ascii=False)),
    )


async def set_puzzle_image_prompt(
    puzzle_date: date,
    image_prompt: str,
    *,
    difficulty: str = "easy",
    image_model: str = DAILY_IMAGE_MODEL,
) -> None:
    difficulty = normalize_daily_difficulty(difficulty)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_puzzles
        SET image_prompt = $3,
            image_model = $4,
            prepared_at = NULL
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty, image_prompt, image_model),
    )


async def set_puzzle_image_asset(
    puzzle_date: date,
    image_file_id: str,
    *,
    difficulty: str = "easy",
    image_model: str = DAILY_IMAGE_MODEL,
) -> None:
    difficulty = normalize_daily_difficulty(difficulty)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_puzzles
        SET image_file_id = $3,
            image_model = $4,
            prepared_at = NULL
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty, image_file_id, image_model),
    )


async def clear_puzzle_image_asset(puzzle_date: date, *, difficulty: str = "easy") -> None:
    difficulty = normalize_daily_difficulty(difficulty)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_puzzles
        SET image_file_id = '',
            prepared_at = NULL
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty),
    )


async def mark_puzzle_prepared(puzzle_date: date, *, difficulty: str = "easy") -> None:
    difficulty = normalize_daily_difficulty(difficulty)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_puzzles
        SET prepared_at = NOW()
        WHERE puzzle_date = $1 AND difficulty = $2
        """,
        (puzzle_date, difficulty),
    )


async def get_or_create_result(user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> DailyResult:
    difficulty = normalize_daily_difficulty(difficulty)
    await ensure_user(user_id)
    rows = await db.db_query(
        """
        INSERT INTO public.crocodile_daily_results (user_id, puzzle_date, difficulty)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, puzzle_date, difficulty) DO UPDATE
            SET updated_at = public.crocodile_daily_results.updated_at
        RETURNING user_id, puzzle_date, difficulty, status, attempts, best_score, used_hints_count,
                  won_at, finished_at, points, share_grid, streak_after
        """,
        (user_id, puzzle_date, difficulty),
    )
    return _row_to_result(rows[0])


async def get_result(user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> DailyResult | None:
    difficulty = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        SELECT user_id, puzzle_date, difficulty, status, attempts, best_score, used_hints_count,
               won_at, finished_at, points, share_grid, streak_after
        FROM public.crocodile_daily_results
        WHERE user_id = $1 AND puzzle_date = $2 AND difficulty = $3
        """,
        (user_id, puzzle_date, difficulty),
    )
    return _row_to_result(rows[0]) if rows else None


async def get_results_for_user(user_id: int, puzzle_date: date) -> dict[str, DailyResult]:
    rows = await db.db_query(
        """
        SELECT user_id, puzzle_date, difficulty, status, attempts, best_score, used_hints_count,
               won_at, finished_at, points, share_grid, streak_after
        FROM public.crocodile_daily_results
        WHERE user_id = $1 AND puzzle_date = $2
        ORDER BY difficulty ASC
        """,
        (user_id, puzzle_date),
    )
    return {normalize_daily_difficulty(row.get("difficulty")): _row_to_result(row) for row in rows}


async def increment_hint_count(user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> int:
    difficulty = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        UPDATE public.crocodile_daily_results
        SET used_hints_count = used_hints_count + 1,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2 AND difficulty = $3 AND status = 'active'
        RETURNING used_hints_count
        """,
        (user_id, puzzle_date, difficulty),
    )
    return int(rows[0]["used_hints_count"]) if rows else 0


async def previous_daily_streak(user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> int:
    difficulty = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        SELECT streak_after
        FROM public.crocodile_daily_results
        WHERE user_id = $1
          AND puzzle_date = ($2::date - 1)
          AND difficulty = $3
          AND status = 'won'
        """,
        (user_id, puzzle_date, difficulty),
    )
    return int(rows[0]["streak_after"]) if rows else 0


async def append_attempt_and_maybe_finish(
    *,
    user_id: int,
    puzzle_date: date,
    difficulty: str = "easy",
    attempt: dict[str, Any],
    max_attempts: int = DAILY_MAX_ATTEMPTS,
) -> DailyResult:
    difficulty = normalize_daily_difficulty(difficulty)
    result = await get_or_create_result(user_id, puzzle_date, difficulty=difficulty)
    if result.status != "active":
        return result

    attempts = [*result.attempts, attempt]
    won = attempt.get("status") == "exact_match"
    lost = len(attempts) >= max_attempts and not won
    status = "won" if won else "lost" if lost else "active"
    best_score = max(result.best_score, float(attempt.get("score") or 0))
    finished = won or lost
    points = compute_points(won=won, attempt_count=len(attempts), used_hints_count=result.used_hints_count)
    share_grid = build_share_grid(attempts, won=won) if finished else ""
    streak = (await previous_daily_streak(user_id, puzzle_date, difficulty=difficulty)) + 1 if won else 0

    rows = await db.db_query(
        """
        UPDATE public.crocodile_daily_results
        SET status = $4,
            attempts = $5::jsonb,
            best_score = $6,
            won_at = CASE WHEN $7 THEN NOW() ELSE won_at END,
            finished_at = CASE WHEN $8 THEN NOW() ELSE finished_at END,
            points = CASE WHEN $8 THEN $9 ELSE points END,
            share_grid = CASE WHEN $8 THEN $10 ELSE share_grid END,
            streak_after = CASE WHEN $8 THEN $11 ELSE streak_after END,
            updated_at = NOW()
        WHERE user_id = $1 AND puzzle_date = $2 AND difficulty = $3 AND status = 'active'
        RETURNING user_id, puzzle_date, difficulty, status, attempts, best_score, used_hints_count,
                  won_at, finished_at, points, share_grid, streak_after
        """,
        (
            user_id,
            puzzle_date,
            difficulty,
            status,
            json.dumps(attempts, ensure_ascii=False),
            best_score,
            won,
            finished,
            points,
            share_grid,
            streak,
        ),
    )
    if rows:
        return _row_to_result(rows[0])
    return await get_or_create_result(user_id, puzzle_date, difficulty=difficulty)


async def upsert_preference(
    user_id: int,
    *,
    is_subscribed: bool | None = None,
    timezone: str | None = None,
    preferred_local_hour: int | None = None,
) -> dict[str, Any]:
    await ensure_user(user_id)
    tz = normalize_timezone(timezone)
    hour = 13 if preferred_local_hour is None else max(0, min(23, int(preferred_local_hour)))
    rows = await db.db_query(
        """
        INSERT INTO public.crocodile_daily_preferences (
            user_id, is_subscribed, timezone, preferred_local_hour, discovery_snoozed_until
        )
        VALUES ($1, COALESCE($2, FALSE), $3, $4, NULL)
        ON CONFLICT (user_id) DO UPDATE SET
            is_subscribed = COALESCE($2, public.crocodile_daily_preferences.is_subscribed),
            timezone = CASE WHEN $5 THEN EXCLUDED.timezone ELSE public.crocodile_daily_preferences.timezone END,
            preferred_local_hour = CASE WHEN $6 THEN EXCLUDED.preferred_local_hour ELSE public.crocodile_daily_preferences.preferred_local_hour END,
            discovery_snoozed_until = CASE WHEN COALESCE($2, FALSE) THEN NULL ELSE public.crocodile_daily_preferences.discovery_snoozed_until END,
            updated_at = NOW()
        RETURNING user_id, is_subscribed, timezone, preferred_local_hour, last_sent_puzzle_date,
                  last_sent_local_date, discovery_last_sent_at, discovery_snoozed_until
        """,
        (
            user_id,
            is_subscribed,
            tz,
            hour,
            timezone is not None,
            preferred_local_hour is not None,
        ),
    )
    return rows[0]


async def update_timezone_if_known(user_id: int, timezone: str | None) -> None:
    tz = normalize_timezone(timezone)
    if not tz:
        return
    await upsert_preference(user_id, timezone=tz)


async def get_preference(user_id: int) -> dict[str, Any] | None:
    rows = await db.db_query(
        """
        SELECT user_id, is_subscribed, timezone, preferred_local_hour, last_sent_puzzle_date,
               last_sent_local_date,
               discovery_last_sent_at, discovery_snoozed_until
        FROM public.crocodile_daily_preferences
        WHERE user_id = $1
        """,
        (user_id,),
    )
    return rows[0] if rows else None


async def snooze_discovery(user_id: int, *, now: datetime | None = None) -> datetime:
    await ensure_user(user_id)
    until = (now or datetime.now(tz=UTC)) + timedelta(days=DISCOVERY_SNOOZE_DAYS)
    await db.db_query(
        """
        INSERT INTO public.crocodile_daily_preferences (user_id, discovery_snoozed_until)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET
            discovery_snoozed_until = EXCLUDED.discovery_snoozed_until,
            updated_at = NOW()
        """,
        (user_id, until),
    )
    return until


async def mark_discovery_sent(user_id: int, *, now: datetime | None = None) -> None:
    await ensure_user(user_id)
    sent_at = now or datetime.now(tz=UTC)
    await db.db_query(
        """
        INSERT INTO public.crocodile_daily_preferences (user_id, discovery_last_sent_at)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET
            discovery_last_sent_at = EXCLUDED.discovery_last_sent_at,
            updated_at = NOW()
        """,
        (user_id, sent_at),
    )


async def get_discovery_candidates(*, now: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
    current = now or datetime.now(tz=UTC)
    rows = await db.db_query(
        """
        WITH audience AS (
            SELECT user_id FROM public.users WHERE is_authorized = 1
            UNION
            SELECT user_id FROM public.crocodile_player_activity
        )
        SELECT audience.user_id,
               COALESCE(pref.timezone, $1) AS timezone,
               pref.discovery_last_sent_at,
               pref.discovery_snoozed_until,
               COALESCE(pref.is_subscribed, FALSE) AS is_subscribed
        FROM audience
        LEFT JOIN public.crocodile_daily_preferences pref ON pref.user_id = audience.user_id
        WHERE COALESCE(pref.is_subscribed, FALSE) = FALSE
          AND (pref.discovery_snoozed_until IS NULL OR pref.discovery_snoozed_until <= $2)
          AND (pref.discovery_last_sent_at IS NULL OR pref.discovery_last_sent_at <= $2 - INTERVAL '14 days')
        ORDER BY audience.user_id
        LIMIT $3
        """,
        (DEFAULT_TIMEZONE, current, limit),
    )
    due: list[dict[str, Any]] = []
    for row in rows:
        local = current.astimezone(_safe_zoneinfo(row.get("timezone")))
        if local.hour == 13:
            due.append(row)
    return due


async def get_due_deliveries(
    *, puzzle_date: date, now: datetime | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    current = now or datetime.now(tz=UTC)
    rows = await db.db_query(
        """
        SELECT user_id, timezone, preferred_local_hour, last_sent_puzzle_date, last_sent_local_date
        FROM public.crocodile_daily_preferences
        WHERE is_subscribed = TRUE
          AND (last_sent_puzzle_date IS NULL OR last_sent_puzzle_date < $1)
        ORDER BY user_id
        LIMIT $2
        """,
        (puzzle_date, limit),
    )
    due: list[dict[str, Any]] = []
    for row in rows:
        local = local_now(row.get("timezone"), current)
        local_today = local.date()
        preferred_hour = int(row.get("preferred_local_hour") or 13)
        last_local = row.get("last_sent_local_date")
        if last_local and last_local >= local_today:
            continue
        if local.hour >= preferred_hour:
            due.append(row)
    return due


async def mark_daily_sent(
    user_id: int,
    puzzle_date: date,
    *,
    now: datetime | None = None,
    timezone: str | None = None,
) -> None:
    local_date = local_date_for_timezone(timezone, now)
    await db.db_query(
        """
        UPDATE public.crocodile_daily_preferences
        SET last_sent_puzzle_date = $2,
            last_sent_local_date = $3,
            updated_at = NOW()
        WHERE user_id = $1
        """,
        (user_id, puzzle_date, local_date),
    )


async def get_leaderboard(puzzle_date: date, *, difficulty: str = "easy", limit: int = 10) -> list[dict[str, Any]]:
    _ = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        SELECT r.user_id,
               SUM(r.points) AS points,
               COUNT(*) AS completed_modes,
               SUM(
                   CASE WHEN jsonb_typeof(r.attempts) = 'array'
                        THEN jsonb_array_length(r.attempts) ELSE 0 END
               ) AS attempt_count,
               SUM(COALESCE(r.used_hints_count, 0)) AS used_hints_count,
               MIN(COALESCE(r.won_at, r.finished_at)) AS first_completed_at,
               u.display_name
        FROM public.crocodile_daily_results r
        LEFT JOIN public.users u ON u.user_id = r.user_id
        WHERE r.puzzle_date = $1
          AND r.status IN ('won', 'lost')
        GROUP BY r.user_id, u.display_name
        ORDER BY SUM(r.points) DESC,
                 COUNT(*) DESC,
                 SUM(
                     CASE WHEN jsonb_typeof(r.attempts) = 'array'
                          THEN jsonb_array_length(r.attempts) ELSE 0 END
                 ) ASC,
                 MIN(COALESCE(r.won_at, r.finished_at)) ASC NULLS LAST
        LIMIT $2
        """,
        (puzzle_date, limit),
    )
    # Resolve display_name: prefer stored name, fall back to masked ID.
    result: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        uid = int(d.get("user_id") or 0)
        stored_name = (d.get("display_name") or "").strip()
        d["display_name"] = stored_name if stored_name else f"игрок {str(uid)[-4:]}"
        result.append(d)
    return result


async def get_rank(user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> int | None:
    _ = normalize_daily_difficulty(difficulty)
    rows = await db.db_query(
        """
        WITH ranked AS (
            SELECT user_id,
                   ROW_NUMBER() OVER (
                       ORDER BY SUM(points) DESC,
                                COUNT(*) DESC,
                                SUM(
                                    CASE WHEN jsonb_typeof(attempts) = 'array'
                                         THEN jsonb_array_length(attempts) ELSE 0 END
                                ) ASC,
                                MIN(COALESCE(won_at, finished_at)) ASC NULLS LAST
                   ) AS rank
            FROM public.crocodile_daily_results
            WHERE puzzle_date = $1
              AND status IN ('won', 'lost')
            GROUP BY user_id
        )
        SELECT rank FROM ranked WHERE user_id = $2
        """,
        (puzzle_date, user_id),
    )
    return int(rows[0]["rank"]) if rows else None


async def register_result_message(
    *,
    user_id: int,
    puzzle_date: date,
    chat_id: int,
    message_id: int,
    rendered_hash_value: str,
    message_type: str = "text",
) -> None:
    await db.db_query(
        """
        INSERT INTO public.crocodile_daily_result_messages (
            user_id, puzzle_date, chat_id, message_id, rendered_hash, message_type
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (chat_id, message_id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            puzzle_date = EXCLUDED.puzzle_date,
            rendered_hash = EXCLUDED.rendered_hash,
            message_type = EXCLUDED.message_type,
            is_active = TRUE,
            updated_at = NOW()
        """,
        (user_id, puzzle_date, chat_id, message_id, rendered_hash_value, message_type),
    )


async def get_active_result_messages(puzzle_date: date, *, limit: int = 200) -> list[dict[str, Any]]:
    return await db.db_query(
        """
        SELECT id, user_id, puzzle_date, chat_id, message_id, rendered_hash, last_edit_at,
               COALESCE(message_type, 'text') AS message_type
        FROM public.crocodile_daily_result_messages
        WHERE puzzle_date = $1 AND is_active = TRUE
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        (puzzle_date, limit),
    )


async def get_active_result_message_for_user(user_id: int, puzzle_date: date) -> dict[str, Any] | None:
    rows = await db.db_query(
        """
        SELECT id, user_id, puzzle_date, chat_id, message_id, rendered_hash, last_edit_at,
               COALESCE(message_type, 'text') AS message_type
        FROM public.crocodile_daily_result_messages
        WHERE user_id = $1 AND puzzle_date = $2 AND is_active = TRUE
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, puzzle_date),
    )
    return rows[0] if rows else None


async def update_result_message_hash(message_id_pk: int, rendered_hash_value: str) -> None:
    await db.db_query(
        """
        UPDATE public.crocodile_daily_result_messages
        SET rendered_hash = $2,
            last_edit_at = NOW(),
            updated_at = NOW()
        WHERE id = $1
        """,
        (message_id_pk, rendered_hash_value),
    )


async def deactivate_other_result_messages(user_id: int, puzzle_date: date, *, keep_id: int) -> None:
    await db.db_query(
        """
        UPDATE public.crocodile_daily_result_messages
        SET is_active = FALSE,
            updated_at = NOW()
        WHERE user_id = $1
          AND puzzle_date = $2
          AND is_active = TRUE
          AND id <> $3
        """,
        (user_id, puzzle_date, keep_id),
    )


async def deactivate_result_message(message_id_pk: int) -> None:
    await db.db_query(
        """
        UPDATE public.crocodile_daily_result_messages
        SET is_active = FALSE,
            updated_at = NOW()
        WHERE id = $1
        """,
        (message_id_pk,),
    )


# ── Prompt message tracking (placeholder → art swap) ─────────────────────────


async def register_prompt_message(
    *,
    user_id: int,
    puzzle_date: date,
    chat_id: int,
    message_id: int,
) -> None:
    """Store the scheduled delivery photo message so we can swap it later."""
    await db.db_query(
        """
        INSERT INTO public.crocodile_daily_prompt_messages (user_id, puzzle_date, chat_id, message_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, puzzle_date) DO UPDATE SET
            chat_id    = EXCLUDED.chat_id,
            message_id = EXCLUDED.message_id,
            is_active  = TRUE
        """,
        (user_id, puzzle_date, chat_id, message_id),
    )


async def get_active_prompt_message(user_id: int, puzzle_date: date) -> dict[str, Any] | None:
    rows = await db.db_query(
        """
        SELECT id, user_id, puzzle_date, chat_id, message_id
        FROM public.crocodile_daily_prompt_messages
        WHERE user_id = $1 AND puzzle_date = $2 AND is_active = TRUE
        """,
        (user_id, puzzle_date),
    )
    return rows[0] if rows else None


async def deactivate_prompt_message(user_id: int, puzzle_date: date) -> None:
    await db.db_query(
        """
        UPDATE public.crocodile_daily_prompt_messages
        SET is_active = FALSE
        WHERE user_id = $1 AND puzzle_date = $2
        """,
        (user_id, puzzle_date),
    )


# ── Subscription management ───────────────────────────────────────────────────


async def unsubscribe(user_id: int) -> None:
    """Opt a user out of scheduled daily delivery."""
    await db.db_query(
        """
        UPDATE public.crocodile_daily_preferences
        SET is_subscribed = FALSE,
            updated_at    = NOW()
        WHERE user_id = $1
        """,
        (user_id,),
    )


# ── Admin status queries ──────────────────────────────────────────────────────


async def get_delivery_status(puzzle_date: date) -> dict[str, Any]:
    """Return a snapshot of today's delivery pipeline for the admin command."""
    rows = await db.db_query(
        """
        SELECT
            COUNT(*) FILTER (WHERE is_subscribed = TRUE)                          AS total_subscribed,
            COUNT(*) FILTER (WHERE is_subscribed = TRUE
                               AND last_sent_puzzle_date = $1)                    AS sent_today,
            COUNT(*) FILTER (WHERE is_subscribed = TRUE
                               AND (last_sent_puzzle_date IS NULL
                                    OR last_sent_puzzle_date < $1))               AS pending_today
        FROM public.crocodile_daily_preferences
        """,
        (puzzle_date,),
    )
    base = dict(rows[0]) if rows else {"total_subscribed": 0, "sent_today": 0, "pending_today": 0}

    result_rows = await db.db_query(
        """
        SELECT
            COUNT(*) FILTER (WHERE status IN ('won', 'lost'))  AS finished,
            COUNT(*) FILTER (WHERE status = 'won')             AS won,
            COUNT(*) FILTER (WHERE status = 'active')          AS active
        FROM public.crocodile_daily_results
        WHERE puzzle_date = $1
        """,
        (puzzle_date,),
    )
    if result_rows:
        base.update(result_rows[0])

    return base

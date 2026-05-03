from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from telegram import InputFile

from app.games.crocodile_flags import is_daily_dual_track_enabled
from app.repos import crocodile_daily as repo
from app.utils.background_tasks import submit_task

logger = logging.getLogger(__name__)

# -- In-process prep guards (per-worker fast-path) -------------------------
# No eviction cap: ~100 bytes/lock × 730 entries/year = negligible memory.
# Eviction was the root cause of a race condition where actively-held locks
# could be removed, allowing concurrent entry into the critical section.
_PREP_LOCKS: dict[str, asyncio.Lock] = {}


def _prep_lock_key(puzzle_date: date, difficulty: str) -> str:
    return f"{puzzle_date.isoformat()}:{difficulty}"


def _get_local_prep_lock(puzzle_date: date, difficulty: str) -> asyncio.Lock:
    """Return the in-process asyncio.Lock for this slot."""
    key = _prep_lock_key(puzzle_date, difficulty)
    return _PREP_LOCKS.setdefault(key, asyncio.Lock())


async def active_daily_difficulties() -> tuple[str, ...]:
    if await is_daily_dual_track_enabled():
        return repo.DAILY_DIFFICULTIES
    return ("easy",)


def _daily_topic_id(puzzle: repo.DailyPuzzle) -> str:
    return f"daily:{puzzle.puzzle_date}:{puzzle.difficulty}"


def _next_attempt_seq(result: repo.DailyResult) -> int:
    last_seq = 0
    for attempt in result.attempts:
        seq = attempt.get("seq")
        if isinstance(seq, int):
            last_seq = max(last_seq, seq)
    return last_seq + 1


async def ensure_today_puzzle(now: datetime | None = None, *, difficulty: str = "easy") -> repo.DailyPuzzle:
    puzzle_date = repo.today_puzzle_date(now)
    return await repo.create_puzzle_if_missing(puzzle_date, difficulty=repo.normalize_daily_difficulty(difficulty))


async def ensure_today_puzzles(now: datetime | None = None) -> dict[str, repo.DailyPuzzle]:
    puzzle_date = repo.today_puzzle_date(now)
    puzzles: dict[str, repo.DailyPuzzle] = {}
    for difficulty in await active_daily_difficulties():
        puzzles[difficulty] = await repo.create_puzzle_if_missing(puzzle_date, difficulty=difficulty)
    return puzzles


async def get_daily_overview(
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[date, dict[str, repo.DailyPuzzle], dict[str, repo.DailyResult]]:
    await repo.record_player_activity(user_id, event="daily_played")
    puzzle_date = repo.today_puzzle_date(now)
    puzzles: dict[str, repo.DailyPuzzle] = {}
    results: dict[str, repo.DailyResult] = {}

    for difficulty in await active_daily_difficulties():
        puzzle = await repo.create_puzzle_if_missing(puzzle_date, difficulty=difficulty)
        result = await repo.get_or_create_result(user_id, puzzle_date, difficulty=difficulty)
        puzzles[difficulty] = puzzle
        results[difficulty] = result
        _queue_daily_puzzle_preparation_if_needed(puzzle)

    return puzzle_date, puzzles, results


async def get_daily_state(
    user_id: int,
    *,
    difficulty: str = "easy",
    now: datetime | None = None,
) -> tuple[repo.DailyPuzzle, repo.DailyResult]:
    difficulty = repo.normalize_daily_difficulty(difficulty)
    _, puzzles, results = await get_daily_overview(user_id, now=now)
    if difficulty not in puzzles:
        difficulty = "easy"
    return puzzles[difficulty], results[difficulty]


async def get_daily_hints(puzzle: repo.DailyPuzzle) -> list[str]:
    if puzzle.hints:
        return puzzle.hints

    from app.games.hinting import get_or_generate_cached_hints

    hints = await get_or_generate_cached_hints(
        puzzle.target_word,
        puzzle.topic,
        topic_id=_daily_topic_id(puzzle),
        mode="foreground",
    )
    if not hints:
        hints = []
    await repo.set_puzzle_hints(puzzle.puzzle_date, hints, difficulty=puzzle.difficulty)
    return hints


# Default negative prompt applied to all daily puzzle images.
# Prevents text/logos leaking into the image even when the model is
# given a detailed instruction not to include them.
DAILY_NEGATIVE_PROMPT = (
    "text, letters, words, numbers, captions, labels, speech bubbles, "
    "watermark, signature, logo, UI, title, subtitle, handwriting, typography"
)


async def _translate_word_for_prompt(word: str) -> str | None:
    """Translate *word* to English and evaluate its visual form.

    Result is cached in ``word_bank._PROMPT_TRANSLATION_CACHE`` so repeated
    calls never hit the LLM twice for the same word.
    """
    from app.games.word_bank import _PROMPT_TRANSLATION_CACHE
    from app.utils.json_compat import json

    key = word.strip().lower()
    if key in _PROMPT_TRANSLATION_CACHE:
        return _PROMPT_TRANSLATION_CACHE[key]

    prompt = (
        f'Analyze the Russian charades word: "{word}".\n'
        "1. Is it possible to draw this word as a clear physical object or distinct scene? (True/False)\n"
        "2. If True, provide a short English phrase describing how to draw it.\n"
        "3. If False, provide the closest physical metaphor in English.\n"
        "Return ONLY a clean JSON object without Markdown backticks:\n"
        '{"is_drawable": true, "visual_description": "english phrase"}'
    )
    try:
        from app.providers.router import get_provider_router

        router = get_provider_router()
        response_text, _ = await router.get_response(
            preferred_model="opencode-go/minimax-m2.5",
            history=[{"role": "user", "parts": [prompt]}],
            max_key_retries=1,
            timeout=8.0,
        )

        resp_clean = (response_text or "").strip()
        if resp_clean.startswith("```json"):
            resp_clean = resp_clean[7:-3].strip()
        elif resp_clean.startswith("```"):
            resp_clean = resp_clean[3:-3].strip()

        data = json.loads(resp_clean)
        translated = data.get("visual_description", "").strip()
        is_drawable = bool(data.get("is_drawable", True))

        if translated and 2 <= len(translated) <= 150:
            if not is_drawable:
                logger.warning("DailyCroc prompt explicitly marked non-drawable by LLM for word: %s", word)
            _PROMPT_TRANSLATION_CACHE[key] = translated
            return translated
    except Exception as exc:
        logger.debug("Word translation failed for %r: %s", word, exc)

    return None


async def _build_daily_image_prompt(word: str, topic: str, *, difficulty: str) -> str:
    from app.games.word_bank import get_english_equivalent

    en_word = get_english_equivalent(word)
    if not en_word:
        en_word = await _translate_word_for_prompt(word)
    display_word = en_word or word

    tension = (
        "Make the composition immediately readable."
        if difficulty == "easy"
        else "Keep the concept readable but slightly less literal."
    )
    return (
        "Create a vivid polished illustration for a charades game reveal. "
        f'The subject is "{display_word}". '
        "Show the concept clearly and literally, one readable main scene or subject. "
        f"{tension} "
        "Absolutely no text, no letters, no captions, no speech bubbles, no UI, no watermark. "
        "Bright colors, expressive details, clean composition, friendly high-quality digital art."
    )


def _daily_image_seed(puzzle_date: date, difficulty: str) -> int:
    suffix = 1 if difficulty == "easy" else 2
    return int(f"{puzzle_date.strftime('%Y%m%d')}{suffix}")


async def _generate_daily_image_file_id(
    bot,
    *,
    prompt: str,
    puzzle_date: date,
    difficulty: str,
) -> str | None:
    from app.config import settings
    from app.providers.pollinations import get_pollinations_provider

    admin_id = getattr(settings, "ADMIN_ID", None)
    if not admin_id:
        logger.warning("daily puzzle image skipped for %s/%s: ADMIN_ID is not configured", puzzle_date, difficulty)
        return None

    provider = get_pollinations_provider()
    result = await provider.generate(
        prompt=prompt,
        model=repo.DAILY_IMAGE_MODEL,
        width=1024,
        height=1024,
        seed=_daily_image_seed(puzzle_date, difficulty),
        enhance=False,
        negative_prompt=DAILY_NEGATIVE_PROMPT,
    )

    # Alert admin if model was silently substituted (e.g. pollen exhausted → flux)
    if result.warning:
        logger.warning(
            "daily puzzle image warning date=%s difficulty=%s: %s",
            puzzle_date,
            difficulty,
            result.warning,
        )
        # Propagate to admin alerting — callers check this field.
        try:
            from app.admin_alerts import AlertSeverity, alert_admin_raw

            await alert_admin_raw(
                f"⚠️ Daily image model substitution {puzzle_date}/{difficulty}: {result.warning}",
                severity=AlertSeverity.WARNING,
            )
        except Exception:
            pass  # best-effort; logging above is the primary record

    if not result.success or not result.images:
        logger.warning(
            "daily puzzle image generation failed date=%s difficulty=%s model=%s error=%s",
            puzzle_date,
            difficulty,
            repo.DAILY_IMAGE_MODEL,
            result.error_message or "unknown",
        )
        return None

    temp_msg = None
    try:
        temp_msg = await bot.send_photo(
            chat_id=admin_id,
            photo=InputFile(
                io.BytesIO(result.images[0]),
                filename=f"daily-{puzzle_date.isoformat()}-{difficulty}.jpg",
            ),
        )
        if not getattr(temp_msg, "photo", None):
            logger.warning("daily puzzle image upload returned no photo sizes for %s/%s", puzzle_date, difficulty)
            return None
        return temp_msg.photo[-1].file_id
    finally:
        if temp_msg is not None:
            with contextlib.suppress(Exception):
                await temp_msg.delete()


async def prepare_daily_puzzle(
    puzzle_date: date,
    bot=None,
    *,
    difficulty: str = "easy",
    include_image: bool = True,
    force_image: bool = False,
) -> repo.DailyPuzzle:
    difficulty = repo.normalize_daily_difficulty(difficulty)
    local_lock = _get_local_prep_lock(puzzle_date, difficulty)

    # Fast-path: if this process is already preparing the same slot, wait locally.
    # If not, acquire the distributed Redis lock so other workers don't duplicate work.
    async with local_lock:
        from app.cache import redis_client

        _redis_lock_key = f"daily:prep:{puzzle_date.isoformat()}:{difficulty}"
        _redis_lock_ctx = None
        if redis_client:
            try:
                _redis_lock_ctx = redis_client.lock(_redis_lock_key, timeout=180, blocking_timeout=30)
                acquired = await _redis_lock_ctx.acquire()
                if not acquired:
                    # Another worker is already preparing — load whatever it wrote
                    logger.info(
                        "daily prep lock: another worker is preparing date=%s/%s, loading existing",
                        puzzle_date,
                        difficulty,
                    )
                    return await repo.create_puzzle_if_missing(puzzle_date, difficulty=difficulty)
            except Exception as exc:
                logger.warning(
                    "daily prep: Redis lock unavailable, proceeding without dist-lock date=%s/%s: %s",
                    puzzle_date,
                    difficulty,
                    exc,
                )
                _redis_lock_ctx = None

        try:
            puzzle = await repo.create_puzzle_if_missing(puzzle_date, difficulty=difficulty)

            if not puzzle.hints:
                hints = await get_daily_hints(puzzle)
                puzzle = replace(puzzle, hints=hints, prepared_at=None)

            if not puzzle.image_prompt:
                image_prompt = await _build_daily_image_prompt(
                    puzzle.target_word, puzzle.topic, difficulty=puzzle.difficulty
                )
                await repo.set_puzzle_image_prompt(
                    puzzle.puzzle_date,
                    image_prompt,
                    difficulty=puzzle.difficulty,
                    image_model=repo.DAILY_IMAGE_MODEL,
                )
                puzzle = replace(
                    puzzle,
                    image_prompt=image_prompt,
                    image_model=repo.DAILY_IMAGE_MODEL,
                    prepared_at=None,
                )

            if include_image and bot and (force_image or not puzzle.image_file_id):
                image_file_id = await _generate_daily_image_file_id(
                    bot,
                    prompt=puzzle.image_prompt,
                    puzzle_date=puzzle.puzzle_date,
                    difficulty=puzzle.difficulty,
                )
                if image_file_id:
                    await repo.set_puzzle_image_asset(
                        puzzle.puzzle_date,
                        image_file_id,
                        difficulty=puzzle.difficulty,
                        image_model=repo.DAILY_IMAGE_MODEL,
                    )
                    puzzle = replace(
                        puzzle,
                        image_file_id=image_file_id,
                        image_model=repo.DAILY_IMAGE_MODEL,
                        prepared_at=None,
                    )

            if repo.is_puzzle_fully_prepared(puzzle) and not puzzle.prepared_at:
                await repo.mark_puzzle_prepared(puzzle.puzzle_date, difficulty=puzzle.difficulty)
                puzzle = replace(puzzle, prepared_at=datetime.now(tz=UTC))

            return puzzle
        finally:
            if _redis_lock_ctx is not None:
                try:
                    await _redis_lock_ctx.release()
                except Exception as exc:
                    logger.debug("daily prep: Redis lock release failed date=%s/%s: %s", puzzle_date, difficulty, exc)


async def ensure_prepared_puzzles(bot=None, *, now: datetime | None = None) -> list[repo.DailyPuzzle]:
    start_date = repo.today_puzzle_date(now)
    puzzles: list[repo.DailyPuzzle] = []
    difficulties = await active_daily_difficulties()
    for offset in range(repo.DAILY_PREP_DAYS_AHEAD + 1):
        puzzle_date = start_date + timedelta(days=offset)
        for difficulty in difficulties:
            try:
                puzzles.append(
                    await prepare_daily_puzzle(
                        puzzle_date,
                        bot=bot,
                        difficulty=difficulty,
                        include_image=offset <= repo.DAILY_IMAGE_PREP_DAYS_AHEAD,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "daily puzzle pre-generation failed date=%s difficulty=%s: %s",
                    puzzle_date,
                    difficulty,
                    exc,
                )
                fallback = await repo.get_puzzle(puzzle_date, difficulty=difficulty)
                if fallback:
                    puzzles.append(fallback)
    return puzzles


def _queue_daily_puzzle_preparation_if_needed(puzzle: repo.DailyPuzzle) -> None:
    if repo.is_puzzle_fully_prepared(puzzle):
        return
    # Guard: if this worker's local lock is already held for this slot, skip queuing
    # to avoid stacking duplicate background tasks within the same process.
    local_lock = _get_local_prep_lock(puzzle.puzzle_date, puzzle.difficulty)
    if local_lock.locked():
        return
    try:
        from app.bot_instance import get_bot

        bot = get_bot()
        if bot:
            submit_task(
                prepare_daily_puzzle(
                    puzzle.puzzle_date,
                    bot=bot,
                    difficulty=puzzle.difficulty,
                    include_image=True,
                )
            )
    except Exception as exc:
        logger.debug(
            "daily background preparation skip date=%s difficulty=%s: %s",
            puzzle.puzzle_date,
            puzzle.difficulty,
            exc,
        )


def history_items(result: repo.DailyResult, *, after_seq: int = 0) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for attempt in result.attempts:
        seq = int(attempt.get("seq") or 0)
        if after_seq > 0 and seq <= after_seq:
            continue
        items.append(
            {
                "word": attempt.get("word", ""),
                "status": attempt.get("status", ""),
                "hint": attempt.get("hint", ""),
                "score": attempt.get("score", 0),
                "cached": bool(attempt.get("cached", False)),
                "seq": seq,
                "server_time_ms": int(attempt.get("server_time_ms") or 0),
            }
        )
    return items


async def build_daily_completion_summary(
    user_id: int,
    puzzle_date: date,
    *,
    focus_difficulty: str = "easy",
) -> dict[str, Any]:
    focus_difficulty = repo.normalize_daily_difficulty(focus_difficulty)
    difficulties = await active_daily_difficulties()
    puzzles = await repo.get_puzzles_for_date(puzzle_date)
    results = await repo.get_results_for_user(user_id, puzzle_date)
    aggregate_leaderboard = await repo.get_leaderboard(puzzle_date, limit=5)
    aggregate_rank = (
        await repo.get_rank(user_id, puzzle_date) if any(item.status != "active" for item in results.values()) else None
    )

    modes: dict[str, dict[str, Any]] = {}
    next_difficulty: str | None = None
    for difficulty in difficulties:
        puzzle = puzzles.get(difficulty) or await repo.create_puzzle_if_missing(puzzle_date, difficulty=difficulty)
        result = results.get(difficulty) or await repo.get_or_create_result(user_id, puzzle_date, difficulty=difficulty)
        modes[difficulty] = {
            "difficulty": difficulty,
            "label": repo.daily_difficulty_label(difficulty),
            "status": result.status,
            "completed": result.status != "active",
            "attempts": len(result.attempts),
            "max_attempts": repo.DAILY_MAX_ATTEMPTS,
            "points": result.points,
            "streak": result.streak_after,
            "rank": aggregate_rank if result.status != "active" else None,
            "leaderboard": aggregate_leaderboard,
            "share_grid": result.share_grid,
            "word": puzzle.target_word if result.status != "active" else "",
        }
        if next_difficulty is None and difficulty != focus_difficulty and result.status == "active":
            next_difficulty = difficulty

    if focus_difficulty not in modes:
        focus_difficulty = next(iter(modes), "easy")

    return {
        "puzzle_date": puzzle_date.isoformat(),
        "focus_difficulty": focus_difficulty,
        "next_difficulty": next_difficulty,
        "modes": modes,
    }


def _completed_event(
    puzzle: repo.DailyPuzzle,
    result: repo.DailyResult,
    summary: dict[str, Any],
) -> dict[str, Any]:
    focus = summary.get("modes", {}).get(puzzle.difficulty, {})
    return {
        "event": "daily_completed",
        "status": result.status,
        "difficulty": puzzle.difficulty,
        "word": puzzle.target_word,
        "attempts": len(result.attempts),
        "max_attempts": repo.DAILY_MAX_ATTEMPTS,
        "points": result.points,
        "streak": result.streak_after,
        "share_grid": result.share_grid,
        "won": result.status == "won",
        "rank": focus.get("rank"),
        "leaderboard": focus.get("leaderboard", []),
        "modes": list(summary.get("modes", {}).values()),
        "next_difficulty": summary.get("next_difficulty"),
        "focus_difficulty": summary.get("focus_difficulty"),
    }


async def process_daily_guess(
    user_id: int,
    word: str,
    *,
    difficulty: str = "easy",
    now: datetime | None = None,
) -> dict[str, Any]:
    from app.games.judge import judge_guess, score_bar, score_emoji

    difficulty = repo.normalize_daily_difficulty(difficulty)
    word = word.strip()
    if not word:
        return {"event": "error", "message": "Empty guess"}

    puzzle, result = await get_daily_state(user_id, difficulty=difficulty, now=now)
    if result.status != "active" or len(result.attempts) >= repo.DAILY_MAX_ATTEMPTS:
        summary = await build_daily_completion_summary(user_id, puzzle.puzzle_date, focus_difficulty=difficulty)
        return _completed_event(puzzle, result, summary)

    status_str, judgement = await judge_guess(
        puzzle.target_word,
        word,
        category=puzzle.topic,
        topic_id=_daily_topic_id(puzzle),
        sense_context=puzzle.topic,
    )

    if status_str == "judge_unavailable":
        return {
            "event": "judge_unavailable",
            "message": "🤔 Крокодил слишком глубоко задумался... Попытка не засчитана, попробуй ещё раз!",
        }

    prefixed_hint = f"{score_emoji(judgement.score)} {judgement.hint}"
    attempt = {
        "word": word,
        "status": status_str,
        "hint": prefixed_hint,
        "score": judgement.score,
        "cached": judgement.cached,
        "seq": _next_attempt_seq(result),
        "server_time_ms": int(time.time() * 1000),
    }
    updated = await repo.append_attempt_and_maybe_finish(
        user_id=user_id,
        puzzle_date=puzzle.puzzle_date,
        difficulty=puzzle.difficulty,
        attempt=attempt,
        max_attempts=repo.DAILY_MAX_ATTEMPTS,
    )

    event: dict[str, Any] = {
        "event": "result",
        "difficulty": puzzle.difficulty,
        "status": status_str,
        "score": judgement.score,
        "score_bar": score_bar(judgement.score),
        "hint": prefixed_hint,
        "attempts": len(updated.attempts),
        "max_attempts": repo.DAILY_MAX_ATTEMPTS,
        "cached": judgement.cached,
        "best_score": updated.best_score,
        "seq": attempt["seq"],
        "server_time_ms": attempt["server_time_ms"],
    }
    if updated.status in {"won", "lost"}:
        summary = await build_daily_completion_summary(user_id, puzzle.puzzle_date, focus_difficulty=puzzle.difficulty)
        event.update(_completed_event(puzzle, updated, summary))
        if updated.status == "lost":
            event["event"] = "game_over"
            event["reason"] = "max_attempts"
        event["daily_completed"] = True
    return event


def current_puzzle_date_iso(now: datetime | None = None) -> str:
    return repo.today_puzzle_date(now).isoformat()


def is_current_puzzle(puzzle_date: date, now: datetime | None = None) -> bool:
    return puzzle_date == repo.today_puzzle_date(now or datetime.now(tz=UTC))

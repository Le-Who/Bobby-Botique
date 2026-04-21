from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from telegram import InputFile

from app.repos import crocodile_daily as repo
from app.utils.background_tasks import submit_task

logger = logging.getLogger(__name__)
_PREP_LOCKS: dict[str, asyncio.Lock] = {}


async def ensure_today_puzzle(now: datetime | None = None) -> repo.DailyPuzzle:
    puzzle_date = repo.today_puzzle_date(now)
    return await repo.create_puzzle_if_missing(puzzle_date)


async def get_daily_state(user_id: int, *, now: datetime | None = None) -> tuple[repo.DailyPuzzle, repo.DailyResult]:
    await repo.record_player_activity(user_id, event="daily_played")
    puzzle = await ensure_today_puzzle(now)
    result = await repo.get_or_create_result(user_id, puzzle.puzzle_date)
    _queue_daily_puzzle_preparation_if_needed(puzzle)
    return puzzle, result


async def get_daily_hints(puzzle: repo.DailyPuzzle) -> list[str]:
    if puzzle.hints:
        return puzzle.hints

    from app.games.hinting import get_or_generate_cached_hints

    hints = await get_or_generate_cached_hints(
        puzzle.target_word,
        puzzle.topic,
        topic_id=f"daily:{puzzle.puzzle_date}",
        mode="foreground",
    )
    if not hints:
        hints = []
    await repo.set_puzzle_hints(puzzle.puzzle_date, hints)
    return hints


def _prep_lock(puzzle_date: date) -> asyncio.Lock:
    return _PREP_LOCKS.setdefault(puzzle_date.isoformat(), asyncio.Lock())


def _build_daily_image_prompt(word: str, topic: str) -> str:
    return (
        "Create a vivid polished illustration for a charades game reveal. "
        f"The hidden word is '{word}'. Topic/context: '{topic}'. "
        "Show the concept clearly and literally, with one readable main scene or subject. "
        "No text, no letters, no captions, no speech bubbles, no UI, no watermark. "
        "Bright colors, expressive details, clean composition, friendly high-quality digital art."
    )


def _daily_image_seed(puzzle_date: date) -> int:
    return int(puzzle_date.strftime("%Y%m%d"))


async def _generate_daily_image_file_id(bot, *, prompt: str, puzzle_date: date) -> str | None:
    from app.config import settings
    from app.providers.pollinations import get_pollinations_provider

    admin_id = getattr(settings, "ADMIN_ID", None)
    if not admin_id:
        logger.warning("daily puzzle image skipped for %s: ADMIN_ID is not configured", puzzle_date)
        return None

    provider = get_pollinations_provider()
    result = await provider.generate(
        prompt=prompt,
        model=repo.DAILY_IMAGE_MODEL,
        width=1024,
        height=1024,
        seed=_daily_image_seed(puzzle_date),
        enhance=True,
    )
    if not result.success or not result.images:
        logger.warning(
            "daily puzzle image generation failed date=%s model=%s error=%s",
            puzzle_date,
            repo.DAILY_IMAGE_MODEL,
            result.error_message or "unknown",
        )
        return None

    temp_msg = None
    try:
        temp_msg = await bot.send_photo(
            chat_id=admin_id,
            photo=InputFile(io.BytesIO(result.images[0]), filename=f"daily-{puzzle_date.isoformat()}.jpg"),
        )
        if not getattr(temp_msg, "photo", None):
            logger.warning("daily puzzle image upload returned no photo sizes for %s", puzzle_date)
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
    include_image: bool = True,
    force_image: bool = False,
) -> repo.DailyPuzzle:
    async with _prep_lock(puzzle_date):
        puzzle = await repo.create_puzzle_if_missing(puzzle_date)

        if not puzzle.hints:
            hints = await get_daily_hints(puzzle)
            puzzle = replace(puzzle, hints=hints, prepared_at=None)

        if not puzzle.image_prompt:
            image_prompt = _build_daily_image_prompt(puzzle.target_word, puzzle.topic)
            await repo.set_puzzle_image_prompt(
                puzzle.puzzle_date,
                image_prompt,
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
            )
            if image_file_id:
                await repo.set_puzzle_image_asset(
                    puzzle.puzzle_date,
                    image_file_id,
                    image_model=repo.DAILY_IMAGE_MODEL,
                )
                puzzle = replace(
                    puzzle,
                    image_file_id=image_file_id,
                    image_model=repo.DAILY_IMAGE_MODEL,
                    prepared_at=None,
                )

        if repo.is_puzzle_fully_prepared(puzzle) and not puzzle.prepared_at:
            await repo.mark_puzzle_prepared(puzzle.puzzle_date)
            puzzle = replace(puzzle, prepared_at=datetime.now(tz=UTC))

        return puzzle


async def ensure_prepared_puzzles(bot=None, *, now: datetime | None = None) -> list[repo.DailyPuzzle]:
    start_date = repo.today_puzzle_date(now)
    puzzles: list[repo.DailyPuzzle] = []
    for offset in range(repo.DAILY_PREP_DAYS_AHEAD + 1):
        puzzle_date = start_date + timedelta(days=offset)
        try:
            puzzles.append(
                await prepare_daily_puzzle(
                    puzzle_date,
                    bot=bot,
                    include_image=offset <= repo.DAILY_IMAGE_PREP_DAYS_AHEAD,
                )
            )
        except Exception as exc:
            logger.warning("daily puzzle pre-generation failed date=%s: %s", puzzle_date, exc)
            fallback = await repo.get_puzzle(puzzle_date)
            if fallback:
                puzzles.append(fallback)
    return puzzles


def _queue_daily_puzzle_preparation_if_needed(puzzle: repo.DailyPuzzle) -> None:
    if repo.is_puzzle_fully_prepared(puzzle):
        return
    if _prep_lock(puzzle.puzzle_date).locked():
        return
    try:
        from app.bot_instance import get_bot

        bot = get_bot()
        if bot:
            submit_task(prepare_daily_puzzle(puzzle.puzzle_date, bot=bot, include_image=True))
    except Exception as exc:
        logger.debug("daily background preparation skip date=%s: %s", puzzle.puzzle_date, exc)


async def process_daily_guess(user_id: int, word: str, *, now: datetime | None = None) -> dict[str, Any]:
    from app.games.judge import judge_guess, score_bar, score_emoji

    word = word.strip()
    if not word:
        return {"event": "error", "message": "Empty guess"}

    puzzle, result = await get_daily_state(user_id, now=now)
    if result.status != "active":
        return _completed_event(puzzle, result)

    if len(result.attempts) >= repo.DAILY_MAX_ATTEMPTS:
        return _completed_event(puzzle, result)

    status_str, judgement = await judge_guess(
        puzzle.target_word,
        word,
        category=puzzle.topic,
        topic_id=f"daily:{puzzle.puzzle_date}",
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
    }
    updated = await repo.append_attempt_and_maybe_finish(
        user_id=user_id,
        puzzle_date=puzzle.puzzle_date,
        attempt=attempt,
        max_attempts=repo.DAILY_MAX_ATTEMPTS,
    )

    event: dict[str, Any] = {
        "event": "result",
        "status": status_str,
        "score": judgement.score,
        "score_bar": score_bar(judgement.score),
        "hint": prefixed_hint,
        "attempts": len(updated.attempts),
        "max_attempts": repo.DAILY_MAX_ATTEMPTS,
        "cached": judgement.cached,
        "best_score": updated.best_score,
    }
    if updated.status == "won":
        event["word"] = puzzle.target_word
        event["daily_completed"] = True
        event["points"] = updated.points
        event["streak"] = updated.streak_after
        event["share_grid"] = updated.share_grid
    elif updated.status == "lost":
        event.update(
            {
                "event": "game_over",
                "reason": "max_attempts",
                "word": puzzle.target_word,
                "daily_completed": True,
                "points": updated.points,
                "streak": updated.streak_after,
                "share_grid": updated.share_grid,
            }
        )
    return event


def history_items(result: repo.DailyResult) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for attempt in result.attempts:
        items.append(
            {
                "word": attempt.get("word", ""),
                "status": attempt.get("status", ""),
                "hint": attempt.get("hint", ""),
                "score": attempt.get("score", 0),
                "cached": bool(attempt.get("cached", False)),
            }
        )
    return items


def _completed_event(puzzle: repo.DailyPuzzle, result: repo.DailyResult) -> dict[str, Any]:
    return {
        "event": "daily_completed",
        "status": result.status,
        "word": puzzle.target_word,
        "attempts": len(result.attempts),
        "max_attempts": repo.DAILY_MAX_ATTEMPTS,
        "points": result.points,
        "streak": result.streak_after,
        "share_grid": result.share_grid,
        "won": result.status == "won",
    }


def current_puzzle_date_iso(now: datetime | None = None) -> str:
    return repo.today_puzzle_date(now).isoformat()


def is_current_puzzle(puzzle_date: date, now: datetime | None = None) -> bool:
    return puzzle_date == repo.today_puzzle_date(now or datetime.now(tz=UTC))

from __future__ import annotations

import asyncio
import html
import logging
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.repos import crocodile_daily as repo

logger = logging.getLogger(__name__)

_REFRESH_DEBOUNCE_S = 2.0
_REFRESH_MESSAGE_LIMIT = 200
_refresh_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
_pending_bots: dict[str, Any] = {}


def _user_label(user_id: int) -> str:
    text = str(user_id)
    return f"игрок {text[-4:]}"


def _share_line(result: repo.DailyResult) -> str:
    outcome = f"{len(result.attempts)}/{repo.DAILY_MAX_ATTEMPTS}" if result.status == "won" else "X/6"
    return (
        f"🐊 Крокодил дня {result.puzzle_date.isoformat()} · {repo.daily_difficulty_label(result.difficulty)}\n"
        f"{result.share_grid or '⬛'} {outcome}\n"
        f"Очки: {result.points} · Серия: {result.streak_after}"
    )


def _daily_art_caption(puzzle: repo.DailyPuzzle) -> str:
    return (
        "🎨 <b>Иллюстрация слова дня</b>\n"
        f"<b>Режим:</b> {html.escape(repo.daily_difficulty_label(puzzle.difficulty))}\n"
        f"<b>Слово:</b> {html.escape(puzzle.target_word)}\n"
        f"<b>Тема:</b> {html.escape(puzzle.topic)}"
    )


def _rendered_content_for_message_type(text: str, message_type: str) -> str:
    return text[:1024] if message_type == "photo" else text


def _preferred_completion_focus(results: dict[str, repo.DailyResult]) -> str:
    if "hard" in results and results["hard"].status != "active":
        return "hard"
    if "easy" in results and results["easy"].status != "active":
        return "easy"
    if results:
        if "easy" in results:
            return "easy"
        return next(iter(results))
    return "easy"


async def _resolve_completion_focus(user_id: int, puzzle_date: date) -> str:
    results = await repo.get_results_for_user(user_id, puzzle_date)
    return _preferred_completion_focus(results)


async def _load_completion_puzzle_with_art(bot, user_id: int, puzzle_date: date, *, difficulty: str) -> repo.DailyPuzzle | None:
    difficulty = repo.normalize_daily_difficulty(difficulty)
    puzzle = await repo.get_puzzle(puzzle_date, difficulty=difficulty)
    if puzzle and puzzle.image_file_id:
        return puzzle

    try:
        from app.games.crocodile_daily import prepare_daily_puzzle

        refreshed = await prepare_daily_puzzle(
            puzzle_date,
            bot=bot,
            difficulty=difficulty,
            include_image=True,
            force_image=True,
        )
        if refreshed.image_file_id:
            return refreshed
        logger.warning(
            "daily completion art missing after refresh user=%s date=%s difficulty=%s",
            user_id,
            puzzle_date,
            difficulty,
        )
        return refreshed
    except Exception as exc:
        logger.warning(
            "daily completion art prepare failed user=%s date=%s difficulty=%s: %s",
            user_id,
            puzzle_date,
            difficulty,
            exc,
        )
        return puzzle


async def _send_daily_completion_art(bot, user_id: int, puzzle_date: date, *, difficulty: str = "easy") -> bool:
    difficulty = repo.normalize_daily_difficulty(difficulty)
    puzzle = await _load_completion_puzzle_with_art(bot, user_id, puzzle_date, difficulty=difficulty)
    if not puzzle or not puzzle.image_file_id:
        return False

    caption = _daily_art_caption(puzzle)
    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=puzzle.image_file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        return True
    except BadRequest as exc:
        msg = str(exc).lower()
        if "wrong file identifier" not in msg and "file reference" not in msg:
            logger.warning("daily completion art failed user=%s date=%s: %s", user_id, puzzle_date, exc)
            return False
        logger.warning("daily completion art file_id stale for %s: %s", puzzle_date, exc)
    except TelegramError as exc:
        logger.warning("daily completion art failed user=%s date=%s: %s", user_id, puzzle_date, exc)
        return False

    try:
        await repo.clear_puzzle_image_asset(puzzle_date, difficulty=difficulty)
        refreshed = await _load_completion_puzzle_with_art(bot, user_id, puzzle_date, difficulty=difficulty)
        if not refreshed.image_file_id:
            return False
        await bot.send_photo(
            chat_id=user_id,
            photo=refreshed.image_file_id,
            caption=_daily_art_caption(refreshed),
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception as exc:
        logger.warning("daily completion art refresh failed user=%s date=%s: %s", user_id, puzzle_date, exc)
        return False


async def render_daily_result_body(
    user_id: int,
    puzzle_date: date,
    *,
    focus_difficulty: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    from app.games.crocodile_daily import build_daily_completion_summary
    from app.handlers.daily_crocodile import _play_button

    summary = await build_daily_completion_summary(
        user_id,
        puzzle_date,
        focus_difficulty=focus_difficulty or "easy",
    )
    modes = summary.get("modes", {})
    if not modes:
        text = "🐊 <b>Крокодил дня</b>\n\nРезультат пока не найден."
        return text, None

    focus = repo.normalize_daily_difficulty(summary.get("focus_difficulty"))
    focus_mode = modes.get(focus) or next(iter(modes.values()))
    results = await repo.get_results_for_user(user_id, puzzle_date)
    preference = await repo.get_preference(user_id)
    is_subscribed = bool(preference and preference.get("is_subscribed"))

    status_icon = "🎉" if focus_mode.get("status") == "won" else "😔" if focus_mode.get("status") == "lost" else "⏳"
    attempts_label = (
        f"{int(focus_mode.get('attempts') or 0)}/{repo.DAILY_MAX_ATTEMPTS}"
        if focus_mode.get("status") == "won"
        else "X/6" if focus_mode.get("status") == "lost"
        else f"{int(focus_mode.get('attempts') or 0)}/{repo.DAILY_MAX_ATTEMPTS}"
    )
    lines = [
        f"🐊 <b>Крокодил дня</b> · <code>{puzzle_date.isoformat()}</code>",
        "",
        f"{status_icon} <b>{html.escape(repo.daily_difficulty_label(focus_mode.get('difficulty')))}:</b> {html.escape(attempts_label)} · <b>{int(focus_mode.get('points') or 0)}</b> очков",
        f"🔥 <b>Серия:</b> {int(focus_mode.get('streak') or 0)}",
    ]
    if focus_mode.get("rank"):
        lines.append(f"🏁 <b>Место сейчас:</b> #{int(focus_mode['rank'])}")

    lines.extend(["", "🏆 <b>Лидерборд</b>"])
    leaderboard = focus_mode.get("leaderboard") or []
    if leaderboard:
        for idx, row in enumerate(leaderboard, start=1):
            # Prefer display_name stored from Telegram initData; fall back to masked ID.
            stored_name = (row.get("display_name") or "").strip()
            player = html.escape(stored_name) if stored_name else _user_label(int(row["user_id"]))
            lines.append(f"{idx}. {player} — <b>{int(row['points'])}</b>")
    else:
        lines.append("Пока нет завершённых результатов.")

    lines.extend(["", "<b>Статусы режимов</b>"])
    for difficulty, mode in modes.items():
        mode_icon = "✅" if mode.get("completed") else "🕹"
        if mode.get("status") == "won":
            mode_suffix = f"{int(mode.get('attempts') or 0)}/{repo.DAILY_MAX_ATTEMPTS}"
        elif mode.get("status") == "lost":
            mode_suffix = "X/6"
        else:
            mode_suffix = "ещё доступен"
        lines.append(
            f"{mode_icon} {html.escape(repo.daily_difficulty_label(difficulty))} — {html.escape(mode_suffix)}"
        )

    share_lines = [
        f"<code>{html.escape(_share_line(result))}</code>"
        for result in results.values()
        if result.status in {"won", "lost"}
    ]
    if share_lines:
        lines.extend(["", "<b>Поделиться:</b>", *share_lines])

    next_difficulty = summary.get("next_difficulty")
    if next_difficulty:
        lines.extend(
            [
                "",
                f"➡️ <b>Ещё доступно:</b> {html.escape(repo.daily_difficulty_label(next_difficulty))}",
            ]
        )

    keyboard: InlineKeyboardMarkup | None
    rows = [[_play_button("Открыть daily")]]
    if is_subscribed:
        rows.append([InlineKeyboardButton("Передумали? Отписаться", callback_data="dailycroc:unsubscribe")])
    else:
        rows.append([InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")])
    keyboard = InlineKeyboardMarkup(rows)
    return "\n".join(lines), keyboard


async def send_daily_result_message(bot, user_id: int, puzzle_date: date, *, focus_difficulty: str | None = None) -> None:
    text, keyboard = await render_daily_result_body(user_id, puzzle_date, focus_difficulty=focus_difficulty)
    msg = await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    await repo.register_result_message(
        user_id=user_id,
        puzzle_date=puzzle_date,
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        rendered_hash_value=repo.render_hash(text),
    )


async def _update_result_message(
    bot,
    item: dict[str, Any],
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    *,
    puzzle: repo.DailyPuzzle | None = None,
) -> bool:
    message_type = item.get("message_type", "text")
    content = _rendered_content_for_message_type(text, message_type)
    try:
        if message_type == "photo":
            if puzzle and puzzle.image_file_id:
                await bot.edit_message_media(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    media=InputMediaPhoto(
                        media=puzzle.image_file_id,
                        caption=content,
                        parse_mode=ParseMode.HTML,
                    ),
                    reply_markup=keyboard,
                )
            else:
                await bot.edit_message_caption(
                    chat_id=item["chat_id"],
                    message_id=item["message_id"],
                    caption=content,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
        else:
            await bot.edit_message_text(
                chat_id=item["chat_id"],
                message_id=item["message_id"],
                text=content,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        await repo.update_result_message_hash(int(item["id"]), repo.render_hash(content))
        return True
    except RetryAfter as exc:
        await asyncio.sleep(float(getattr(exc, "retry_after", 1.0)))
    except BadRequest as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            await repo.update_result_message_hash(int(item["id"]), repo.render_hash(content))
            return True
        if "message to edit not found" in msg or "message can't be edited" in msg:
            await repo.deactivate_result_message(int(item["id"]))
            return False
        logger.debug("daily result edit bad request: %s", exc)
    except (Forbidden, TelegramError) as exc:
        logger.debug("daily result edit failed: %s", exc)
        await repo.deactivate_result_message(int(item["id"]))
    except Exception as exc:
        logger.debug("daily result refresh failed item=%s: %s", item.get("id"), exc)
    return False


async def send_daily_completion_bundle(
    bot,
    user_id: int,
    puzzle_date: date,
    *,
    focus_difficulty: str = "easy",
) -> None:
    """Send the game-over bundle: swap placeholder photo → real art, or fall back."""
    pref = await repo.get_preference(user_id)

    # Mark as sent so the scheduler won't send a duplicate today.
    try:
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        today = repo.today_puzzle_date(now)
        if puzzle_date == today and not repo.was_daily_delivered_today(pref, puzzle_date=today, now=now):
            await repo.mark_daily_sent(
                user_id,
                today,
                now=now,
                timezone=(pref or {}).get("timezone"),
            )
    except Exception as exc:
        logger.debug("daily: mark_daily_sent failed user=%s: %s", user_id, exc)

    text, keyboard = await render_daily_result_body(user_id, puzzle_date, focus_difficulty=focus_difficulty)
    existing_result = await repo.get_active_result_message_for_user(user_id, puzzle_date)
    if existing_result:
        await repo.deactivate_other_result_messages(user_id, puzzle_date, keep_id=int(existing_result["id"]))
        puzzle = None
        if existing_result.get("message_type") == "photo":
            puzzle = await _load_completion_puzzle_with_art(bot, user_id, puzzle_date, difficulty=focus_difficulty)
        if await _update_result_message(bot, existing_result, text, keyboard, puzzle=puzzle):
            return

    # Try to edit the prompt photo message (placeholder → real art + result caption).
    prompt_msg = await repo.get_active_prompt_message(user_id, puzzle_date)
    puzzle = await _load_completion_puzzle_with_art(bot, user_id, puzzle_date, difficulty=focus_difficulty)
    if prompt_msg and puzzle and puzzle.image_file_id:
        try:
            await bot.edit_message_media(
                chat_id=prompt_msg["chat_id"],
                message_id=prompt_msg["message_id"],
                media=InputMediaPhoto(
                    media=puzzle.image_file_id,
                    caption=_rendered_content_for_message_type(text, "photo"),
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=keyboard,
            )
            await repo.deactivate_prompt_message(user_id, puzzle_date)
            await repo.register_result_message(
                user_id=user_id,
                puzzle_date=puzzle_date,
                chat_id=prompt_msg["chat_id"],
                message_id=prompt_msg["message_id"],
                rendered_hash_value=repo.render_hash(_rendered_content_for_message_type(text, "photo")),
                message_type="photo",
            )
            return
        except Exception as exc:
            logger.warning("daily: swap prompt→art failed user=%s: %s — sending separately", user_id, exc)

    # Fallback: send art as a new photo, then a separate text result message.
    await _send_daily_completion_art(bot, user_id, puzzle_date, difficulty=focus_difficulty)
    await send_daily_result_message(bot, user_id, puzzle_date, focus_difficulty=focus_difficulty)


def queue_daily_result_refresh(bot, puzzle_date: date) -> None:
    key = puzzle_date.isoformat()
    _pending_bots[key] = bot
    task = _refresh_tasks.get(key)
    if task and not task.done():
        return
    _refresh_tasks[key] = asyncio.create_task(_flush_daily_result_refresh(puzzle_date))


async def _flush_daily_result_refresh(puzzle_date: date) -> None:
    key = puzzle_date.isoformat()
    try:
        await asyncio.sleep(_REFRESH_DEBOUNCE_S)
        bot = _pending_bots.pop(key, None)
        if bot is None:
            return

        messages = await repo.get_active_result_messages(puzzle_date, limit=_REFRESH_MESSAGE_LIMIT)
        for item in messages:
            try:
                user_id = int(item["user_id"])
                focus_difficulty = await _resolve_completion_focus(user_id, puzzle_date)
                text, keyboard = await render_daily_result_body(
                    user_id,
                    puzzle_date,
                    focus_difficulty=focus_difficulty,
                )
                msg_type = item.get("message_type", "text")
                content = _rendered_content_for_message_type(text, msg_type)
                text_hash = repo.render_hash(content)
                if text_hash == item.get("rendered_hash"):
                    continue
                puzzle = None
                if msg_type == "photo":
                    puzzle = await _load_completion_puzzle_with_art(bot, user_id, puzzle_date, difficulty=focus_difficulty)
                updated = await _update_result_message(bot, item, text, keyboard, puzzle=puzzle)
                if updated:
                    await repo.deactivate_other_result_messages(user_id, puzzle_date, keep_id=int(item["id"]))
                await asyncio.sleep(0.05)
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 1.0)))
                _pending_bots[key] = bot
            except Exception as exc:
                logger.debug("daily result refresh failed item=%s: %s", item.get("id"), exc)
    except asyncio.CancelledError:
        raise
    finally:
        _refresh_tasks.pop(key, None)
        if key in _pending_bots:
            _refresh_tasks[key] = asyncio.create_task(_flush_daily_result_refresh(puzzle_date))


def reset_daily_telegram_state_for_tests() -> None:
    for task in list(_refresh_tasks.values()):
        if not task.done():
            task.cancel()
    _refresh_tasks.clear()
    _pending_bots.clear()

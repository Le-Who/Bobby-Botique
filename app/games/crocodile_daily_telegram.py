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


def _attempt_count(row_or_result: Any) -> int:
    if isinstance(row_or_result, repo.DailyResult):
        return len(row_or_result.attempts)
    return int(row_or_result.get("attempt_count") or 0)


def _share_line(result: repo.DailyResult) -> str:
    outcome = f"{len(result.attempts)}/{repo.DAILY_MAX_ATTEMPTS}" if result.status == "won" else "X/6"
    return (
        f"🐊 Крокодил дня {result.puzzle_date.isoformat()}\n"
        f"{result.share_grid or '⬛'} {outcome}\n"
        f"Очки: {result.points} · Серия: {result.streak_after}"
    )


def _daily_art_caption(puzzle: repo.DailyPuzzle) -> str:
    return (
        "🎨 <b>Иллюстрация слова дня</b>\n"
        f"<b>Слово:</b> {html.escape(puzzle.target_word)}\n"
        f"<b>Тема:</b> {html.escape(puzzle.topic)}"
    )


async def _send_daily_completion_art(bot, user_id: int, puzzle_date: date) -> bool:
    puzzle = await repo.get_puzzle(puzzle_date)
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
        from app.games.crocodile_daily import prepare_daily_puzzle

        await repo.clear_puzzle_image_asset(puzzle_date)
        refreshed = await prepare_daily_puzzle(puzzle_date, bot=bot, include_image=True, force_image=True)
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


async def render_daily_result_body(user_id: int, puzzle_date: date) -> tuple[str, InlineKeyboardMarkup | None]:
    result = await repo.get_result(user_id, puzzle_date)
    if not result:
        text = "🐊 <b>Крокодил дня</b>\n\nРезультат пока не найден."
        return text, None

    rank = await repo.get_rank(user_id, puzzle_date)
    leaderboard = await repo.get_leaderboard(puzzle_date, limit=5)
    preference = await repo.get_preference(user_id)
    is_subscribed = bool(preference and preference.get("is_subscribed"))

    status_icon = "🎉" if result.status == "won" else "😔"
    attempts_label = f"{len(result.attempts)}/{repo.DAILY_MAX_ATTEMPTS}" if result.status == "won" else "X/6"
    lines = [
        f"🐊 <b>Крокодил дня</b> · <code>{result.puzzle_date.isoformat()}</code>",
        "",
        f"{status_icon} <b>Ваш результат:</b> {html.escape(attempts_label)} · <b>{result.points}</b> очков",
        f"🔥 <b>Серия:</b> {result.streak_after}",
    ]
    if rank:
        lines.append(f"🏁 <b>Место сейчас:</b> #{rank}")

    lines.extend(["", "🏆 <b>Топ сегодня</b>"])
    if leaderboard:
        for idx, row in enumerate(leaderboard, start=1):
            row_attempts = _attempt_count(row)
            suffix = f"{row_attempts}/{repo.DAILY_MAX_ATTEMPTS}" if row.get("status") == "won" else "X/6"
            lines.append(
                f"{idx}. {_user_label(int(row['user_id']))} — <b>{int(row['points'])}</b> · {html.escape(suffix)}"
            )
    else:
        lines.append("Пока нет завершённых результатов.")

    lines.extend(["", "<b>Поделиться:</b>", f"<code>{html.escape(_share_line(result))}</code>"])

    keyboard: InlineKeyboardMarkup | None
    if is_subscribed:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Передумали? Отписаться", callback_data="dailycroc:unsubscribe")]]
        )
    else:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")]]
        )
    return "\n".join(lines), keyboard


async def send_daily_result_message(bot, user_id: int, puzzle_date: date) -> None:
    text, keyboard = await render_daily_result_body(user_id, puzzle_date)
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


async def send_daily_completion_bundle(bot, user_id: int, puzzle_date: date) -> None:
    """Send the game-over bundle: swap placeholder photo → real art, or fall back."""
    pref = await repo.get_preference(user_id)

    # Mark as sent so the scheduler won't send a duplicate today.
    try:
        today = repo.today_puzzle_date()
        if puzzle_date == today:
            last_sent = pref.get("last_sent_puzzle_date") if pref else None
            if not last_sent or last_sent < today:
                await repo.mark_daily_sent(user_id, today)
    except Exception as exc:
        logger.debug("daily: mark_daily_sent failed user=%s: %s", user_id, exc)

    text, keyboard = await render_daily_result_body(user_id, puzzle_date)

    # Try to edit the prompt photo message (placeholder → real art + result caption).
    prompt_msg = await repo.get_active_prompt_message(user_id, puzzle_date)
    puzzle = await repo.get_puzzle(puzzle_date)
    if prompt_msg and puzzle and puzzle.image_file_id:
        caption = text[:1024]  # Telegram photo caption hard limit
        try:
            await bot.edit_message_media(
                chat_id=prompt_msg["chat_id"],
                message_id=prompt_msg["message_id"],
                media=InputMediaPhoto(
                    media=puzzle.image_file_id,
                    caption=caption,
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
                rendered_hash_value=repo.render_hash(caption),
                message_type="photo",
            )
            return
        except Exception as exc:
            logger.warning("daily: swap prompt→art failed user=%s: %s — sending separately", user_id, exc)

    # Fallback: send art as a new photo, then a separate text result message.
    await _send_daily_completion_art(bot, user_id, puzzle_date)
    await send_daily_result_message(bot, user_id, puzzle_date)


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
                text, keyboard = await render_daily_result_body(int(item["user_id"]), puzzle_date)
                msg_type = item.get("message_type", "text")
                content = text[:1024] if msg_type == "photo" else text
                text_hash = repo.render_hash(content)
                if text_hash == item.get("rendered_hash"):
                    continue
                if msg_type == "photo":
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
                await repo.update_result_message_hash(int(item["id"]), text_hash)
                await asyncio.sleep(0.05)
            except RetryAfter as exc:
                await asyncio.sleep(float(getattr(exc, "retry_after", 1.0)))
                _pending_bots[key] = bot
            except BadRequest as exc:
                msg = str(exc).lower()
                if "message is not modified" in msg:
                    continue
                if "message to edit not found" in msg or "message can't be edited" in msg:
                    await repo.deactivate_result_message(int(item["id"]))
                else:
                    logger.debug("daily result edit bad request: %s", exc)
            except (Forbidden, TelegramError) as exc:
                logger.debug("daily result edit failed: %s", exc)
                await repo.deactivate_result_message(int(item["id"]))
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

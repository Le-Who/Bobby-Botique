from __future__ import annotations

import html
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from app.games import cover_photo
from app.repos import daily_trivia as repo


def _format_duration(elapsed_ms: int) -> str:
    total_seconds = max(0, int(elapsed_ms // 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _user_label(user_id: int) -> str:
    return f"игрок {str(user_id)[-4:]}"


def _month_key(puzzle_date: date) -> str:
    return puzzle_date.strftime("%Y-%m")


async def _get_cover_photo() -> str | InputFile | None:
    return await cover_photo.get_cover_photo("dailytrivia")


async def _remember_cover_file_id(message: Any) -> None:
    await cover_photo.remember_cover_file_id("dailytrivia", message)


def _play_button(label: str = "🧠 Пройти снова") -> InlineKeyboardButton:
    """Re-use the URL from the handler module to avoid circular imports."""
    from app.handlers.daily_trivia import daily_trivia_play_button
    return daily_trivia_play_button(label)


async def render_result_body(
    user_id: int,
    puzzle_date: date,
) -> tuple[str, InlineKeyboardMarkup]:
    result = await repo.get_or_create_result(user_id, puzzle_date)
    leaderboard = await repo.get_daily_leaderboard(puzzle_date, limit=5)

    if result.status != "completed":
        # Game still in progress — return current state
        answered = result.current_question
        text = (
            f"🧠 <b>Daily Trivia</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
            f"📊 <b>Прогресс:</b> {answered}/5 вопросов\n"
            f"⭐ <b>Очки:</b> {result.final_score}"
        )
        keyboard = InlineKeyboardMarkup([[_play_button("🧠 Продолжить")]])
        return text, keyboard

    # Game finished
    rank_rows = await repo.get_daily_leaderboard(puzzle_date, limit=100)
    rank = None
    for idx, row in enumerate(rank_rows, start=1):
        if int(row.get("user_id", 0)) == user_id:
            rank = idx
            break

    rank_line = f"\n🏆 <b>Место дня:</b> #{rank}" if rank else ""

    lines = [
        f"🧠 <b>Daily Trivia</b> · <code>{puzzle_date.isoformat()}</code>",
        "",
        f"🏁 <b>Результат:</b> <b>{result.final_score}</b> очков{rank_line}",
        f"✅ <b>Правильных:</b> {result.correct_count}/5",
        f"⚡ <b>Время:</b> {_format_duration(result.elapsed_ms)}",
        "",
        "🏆 <b>Лучшие сегодня</b>",
    ]
    if leaderboard:
        for index, row in enumerate(leaderboard, start=1):
            name = html.escape(
                (row.get("name") or "").strip()
                or _user_label(int(row["user_id"]))
            )
            lines.append(
                f"{index}. {name} — <b>{int(row['score'])}</b> · "
                f"{int(row['correct'])}/5 правильных"
            )
    else:
        lines.append("Пока нет завершённых результатов.")

    lines.extend(["", "Возвращайтесь завтра за новыми вопросами! 📅"])

    keyboard = InlineKeyboardMarkup(
        [
            [_play_button("🧠 Открыть Trivia")],
            [
                InlineKeyboardButton(
                    "Лучшие за месяц",
                    callback_data=f"dailytrivia:month:{_month_key(puzzle_date)}",
                )
            ],
        ]
    )
    return "\n".join(lines), keyboard


async def _edit_prompt_to_result(
    bot,
    prompt: dict[str, Any],
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> bool:
    """Try to edit the original invite message with the result. Returns True on success."""
    try:
        cover = await _get_cover_photo()
        if cover:
            await bot.edit_message_media(
                chat_id=prompt["chat_id"],
                message_id=prompt["message_id"],
                media=InputMediaPhoto(
                    media=cover,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                ),
                reply_markup=keyboard,
            )
        else:
            await bot.edit_message_text(
                chat_id=prompt["chat_id"],
                message_id=prompt["message_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        return True
    except BadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            return True
        if "there is no text" not in message and "message to edit not found" not in message:
            # Fallback: try plain text edit if media edit failed
            try:
                await bot.edit_message_text(
                    chat_id=prompt["chat_id"],
                    message_id=prompt["message_id"],
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                return True
            except TelegramError:
                return False
    except (OSError, TelegramError):
        return False
    return False


async def _send_result_photo(
    bot,
    user_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Fallback: send a new result message if we can't edit the original."""
    cover = await _get_cover_photo()
    if cover:
        try:
            message = await bot.send_photo(
                chat_id=user_id,
                photo=cover,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            await _remember_cover_file_id(message)
            return
        except (OSError, TelegramError):
            pass

    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def send_trivia_result_message(bot, user_id: int, puzzle_date: date) -> None:
    """Edit the daily invite message to show the final result.

    After editing the triggering user's message, also refreshes all other
    players' result messages that are stale (haven't been updated in 10+ min).
    This keeps everyone's leaderboard up to date without flooding the API.

    Flow:
      1. Render result text + keyboard for user_id.
      2. Look up their stored prompt message and try to edit it in-place.
      3. If no stored message (or edit fails), send a new result message.
      4. Mark the prompt as refreshed (keep active for future refreshes).
      5. Batch-refresh all other stale active result messages for today.
    """
    text, keyboard = await render_result_body(user_id, puzzle_date)
    prompt = await repo.get_active_prompt_message(user_id, puzzle_date)
    edited = False
    if prompt:
        edited = await _edit_prompt_to_result(bot, prompt, text, keyboard)
        if edited:
            await repo.mark_prompt_refreshed(user_id, puzzle_date)
        else:
            await repo.deactivate_prompt_message(user_id, puzzle_date)
    if not edited:
        await _send_result_photo(bot, user_id, text, keyboard)

    # Batch-refresh all other players' result messages that are stale (>10 min).
    await refresh_stale_result_messages(bot, puzzle_date, exclude_user_id=user_id)


# ---------------------------------------------------------------------------
# Throttled leaderboard refresh for all players
# ---------------------------------------------------------------------------

_REFRESH_STALE_SECONDS = 600  # 10 minutes


async def refresh_stale_result_messages(
    bot,
    puzzle_date: date,
    *,
    exclude_user_id: int | None = None,
    stale_seconds: int = _REFRESH_STALE_SECONDS,
) -> None:
    """Edit result messages of all players whose leaderboard view is stale.

    Called after each new game completion so that players who finished earlier
    see an updated leaderboard without having to re-send /trivia.

    Throttle: each player's message is refreshed at most once per stale_seconds
    (default 10 min).  Messages that cannot be edited are deactivated so we
    stop trying.

    Args:
        exclude_user_id: skip this user (they were just handled by the caller).
        stale_seconds: minimum seconds since last refresh.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    stale_prompts = await repo.get_stale_active_prompts(puzzle_date, stale_seconds)

    for prompt in stale_prompts:
        uid = int(prompt["user_id"])
        if uid == exclude_user_id:
            continue
        try:
            text, keyboard = await render_result_body(uid, puzzle_date)
            ok = await _edit_prompt_to_result(bot, prompt, text, keyboard)
            if ok:
                await repo.mark_prompt_refreshed(uid, puzzle_date)
            else:
                await repo.deactivate_prompt_message(uid, puzzle_date)
        except Exception as exc:
            _log.warning("trivia: stale refresh failed user=%s: %s", uid, exc)
            try:
                await repo.deactivate_prompt_message(uid, puzzle_date)
            except Exception:
                pass

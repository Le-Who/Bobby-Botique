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

    Mirrors daily_2048_telegram.send_daily2048_result_message:
      1. Render the result text + keyboard.
      2. Look up the stored prompt message and try to edit it in-place.
      3. If no stored message (or edit fails), send a new result message.
      4. Deactivate the stored prompt record to avoid double-edits.
    """
    text, keyboard = await render_result_body(user_id, puzzle_date)
    prompt = await repo.get_active_prompt_message(user_id, puzzle_date)
    edited = False
    if prompt:
        edited = await _edit_prompt_to_result(bot, prompt, text, keyboard)
        await repo.deactivate_prompt_message(user_id, puzzle_date)
    if not edited:
        await _send_result_photo(bot, user_id, text, keyboard)

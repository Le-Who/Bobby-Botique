from __future__ import annotations

import html
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from app.games import daily_2048
from app.repos import daily_2048 as repo


def _format_duration(elapsed_ms: int) -> str:
    total_seconds = max(0, int(elapsed_ms // 1000))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}:{seconds:02d}"


def _month_key(puzzle_date: date) -> str:
    return puzzle_date.strftime("%Y-%m")


def _user_label(user_id: int) -> str:
    return f"игрок {str(user_id)[-4:]}"


async def render_result_body(user_id: int, puzzle_date: date) -> tuple[str, InlineKeyboardMarkup]:
    from app.handlers.daily_2048 import daily2048_play_button

    result = await repo.get_result(user_id, puzzle_date)
    if not result:
        text = "🎲 <b>Daily 2048 Sprint</b>\n\nРезультат пока не найден."
        keyboard = InlineKeyboardMarkup([[daily2048_play_button("Открыть 2048")]])
        return text, keyboard

    rank = await repo.get_rank(user_id, puzzle_date) if result.status == "won" else None
    leaderboard = await repo.get_leaderboard(puzzle_date, limit=5)
    status_icon = "🏁" if result.status == "won" else "⛔"
    rank_line = f"\n🏆 <b>Место дня:</b> #{rank}" if rank else ""

    lines = [
        f"🎲 <b>Daily 2048 Sprint</b> · <code>{puzzle_date.isoformat()}</code>",
        "",
        f"{status_icon} <b>Результат:</b> <b>{result.final_score}</b> очков{rank_line}",
        f"⚡ <b>Скорость:</b> {_format_duration(result.elapsed_ms)}",
        f"↔️ <b>Ходы до решения:</b> {result.moves}",
        f"✨ <b>Merge score:</b> {result.merge_score}",
        "",
        "🏆 <b>Лучшие сегодня</b>",
    ]
    if leaderboard:
        for index, row in enumerate(leaderboard, start=1):
            name = html.escape((row.get("display_name") or "").strip() or _user_label(int(row["user_id"])))
            lines.append(
                f"{index}. {name} — <b>{int(row['final_score'])}</b> · "
                f"{int(row['moves'])} ходов"
            )
    else:
        lines.append("Пока нет завершённых результатов.")

    if result.status == "won":
        lines.extend(
            [
                "",
                "Можно продолжить тренировку, но рекорд дня уже зафиксирован.",
            ]
        )

    keyboard = InlineKeyboardMarkup(
        [
            [daily2048_play_button("Открыть 2048")],
            [
                InlineKeyboardButton(
                    "Лучшие за месяц",
                    callback_data=f"daily2048:month:{_month_key(puzzle_date)}",
                )
            ],
        ]
    )
    return "\n".join(lines), keyboard


async def send_daily2048_result_message(bot, user_id: int, puzzle_date: date) -> None:
    text, keyboard = await render_result_body(user_id, puzzle_date)
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def render_completion_event(user_id: int, puzzle_date: date) -> dict:
    result = await repo.get_result(user_id, puzzle_date)
    rank = await repo.get_rank(user_id, puzzle_date) if result and result.status == "won" else None
    board = await repo.get_leaderboard(puzzle_date, limit=5)
    return {
        "rank": rank,
        "leaderboard": board,
        "puzzle_date": puzzle_date.isoformat(),
        "goal": daily_2048.goal_payload(await repo.ensure_puzzle(puzzle_date)),
    }

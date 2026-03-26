"""Reminder command and delivery system.

Provides the ``/remind`` command for users to set time-based follow-ups,
and the ``check_and_deliver_reminders`` job for the bot scheduler.
"""

import logging
import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from app.config import UTC_TZ
from app.repos.reminders import (
    create_reminder,
    get_pending_reminders,
    get_user_reminders,
    mark_delivered,
)
from app.utils.decorators import authorized_only, safe_handler
from app.utils.formatting import TelegramFormatter

logger = logging.getLogger(__name__)

# Time parsing patterns: "30m", "2h", "1d", "90min", "3 hours"
_TIME_PATTERN = re.compile(
    r"^(\d+)\s*"
    r"(m|min|mins|minutes|мин|минут[аыу]?"
    r"|h|hr|hrs|hours|час|часа|часов"
    r"|d|day|days|день|дня|дней)"
    r"\s+(.+)$",
    re.IGNORECASE,
)

_UNIT_TO_SECONDS = {
    "m": 60, "min": 60, "mins": 60, "minutes": 60, "мин": 60,
    "минута": 60, "минуты": 60, "минуту": 60, "минут": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hours": 3600,
    "час": 3600, "часа": 3600, "часов": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "день": 86400, "дня": 86400, "дней": 86400,
}


def _parse_reminder_args(text: str) -> tuple[timedelta | None, str | None]:
    """Parse reminder text into (timedelta, prompt) or (None, None)."""
    match = _TIME_PATTERN.match(text.strip())
    if not match:
        return None, None

    amount = int(match.group(1))
    unit = match.group(2).lower()
    prompt = match.group(3).strip()

    seconds = _UNIT_TO_SECONDS.get(unit)
    if not seconds or amount <= 0 or amount > 365 * 24:  # max 1 year
        return None, None

    return timedelta(seconds=amount * seconds), prompt


@authorized_only
@safe_handler("❌ Ошибка создания напоминания.")
async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a reminder: /remind 30m Проверить результаты."""
    user_id = update.effective_user.id

    # No args → show usage + pending reminders
    if not context.args:
        pending = await get_user_reminders(user_id, limit=5)
        text_parts = [
            "⏰ **Напоминания**\n\n"
            "**Формат:** `/remind <время> <текст>`\n"
            "**Примеры:**\n"
            "• `/remind 30m Проверить результаты`\n"
            "• `/remind 2h Написать отчёт`\n"
            "• `/remind 1d Созвон с командой`\n",
        ]
        if pending:
            text_parts.append("\n📋 **Ваши напоминания:**\n")
            for r in pending:
                trigger = r["trigger_at"]
                time_str = trigger.strftime("%d.%m %H:%M") if hasattr(trigger, "strftime") else str(trigger)[:16]
                prompt_preview = r["prompt"][:50] + ("…" if len(r["prompt"]) > 50 else "")
                text_parts.append(f"  • `{time_str}` — {prompt_preview}\n")

        text = "".join(text_parts)
        formatted_text, parse_mode = TelegramFormatter.format_text(text)
        await update.message.reply_text(formatted_text, parse_mode=parse_mode)
        return

    # Parse time + prompt
    raw_text = " ".join(context.args)
    delta, prompt = _parse_reminder_args(raw_text)

    if not delta or not prompt:
        await update.message.reply_text(
            "❌ Не удалось разобрать формат.\n"
            "Используйте: `/remind 30m Текст напоминания`\n"
            "Единицы: m/мин, h/час, d/день",
            parse_mode="Markdown",
        )
        return

    trigger_at = datetime.now(UTC_TZ) + delta
    reminder_id = await create_reminder(user_id, trigger_at, prompt)

    if not reminder_id:
        await update.message.reply_text("❌ Не удалось сохранить напоминание. Попробуйте позже.")
        return

    # Friendly time display
    if delta.total_seconds() < 3600:
        time_label = f"{int(delta.total_seconds() / 60)} мин"
    elif delta.total_seconds() < 86400:
        hours = delta.total_seconds() / 3600
        time_label = f"{hours:.0f} ч" if hours == int(hours) else f"{hours:.1f} ч"
    else:
        days = delta.total_seconds() / 86400
        time_label = f"{days:.0f} д" if days == int(days) else f"{days:.1f} д"

    formatted_text, parse_mode = TelegramFormatter.format_text(
        f"✅ Напоминание через **{time_label}**:\n\n_{prompt}_"
    )
    await update.message.reply_text(formatted_text, parse_mode=parse_mode)


async def check_and_deliver_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback: poll pending reminders and deliver them via bot.

    Called by job_queue.run_repeating every 60 seconds.
    """
    try:
        pending = await get_pending_reminders()
        if not pending:
            return

        for reminder in pending:
            try:
                user_id = reminder["user_id"]
                prompt = reminder["prompt"]

                text = f"⏰ **Напоминание**\n\n{prompt}"
                formatted_text, parse_mode = TelegramFormatter.format_text(text)

                await context.bot.send_message(
                    chat_id=user_id,
                    text=formatted_text,
                    parse_mode=parse_mode,
                )
                await mark_delivered(reminder["id"])
                logger.info("Reminder %d delivered to user %s", reminder["id"], user_id)

            except Exception as e:
                logger.warning("Failed to deliver reminder %d: %s", reminder["id"], e)

    except Exception as e:
        logger.error("Reminder poll loop error: %s", e)

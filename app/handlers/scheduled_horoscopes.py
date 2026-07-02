"""Scheduled horoscope delivery — runs every minute via PTB job_queue.

For each minute tick:
  1. Check active subscriptions due for 'today' delivery (morning).
  2. Check active subscriptions due for 'tomorrow' delivery (evening).
  3. For each due subscription, generate the horoscope via _handle_horoscope
     and send it as a DM to the user.
  4. Mark the subscription as sent so we don't double-deliver.

Horoscope text is generated exactly as the inline handler does, reusing
``app.intent_router._handle_horoscope``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from telegram.ext import ContextTypes

from app.repos.horoscope_subscriptions import (
    get_due_horoscope_subscriptions,
    mark_horoscope_sent,
)

logger = logging.getLogger(__name__)

# Human-readable sign labels (mirrors horoscope_subscription.py; kept local to avoid circular import)
_SIGN_LABELS: dict[str, str] = {
    "aries":       "♈ Овен",
    "taurus":      "♉ Телец",
    "gemini":      "♊ Близнецы",
    "cancer":      "♋ Рак",
    "leo":         "♌ Лев",
    "virgo":       "♍ Дева",
    "libra":       "♎ Весы",
    "scorpio":     "♏ Скорпион",
    "sagittarius": "♐ Стрелец",
    "capricorn":   "♑ Козерог",
    "aquarius":    "♒ Водолей",
    "pisces":      "♓ Рыбы",
}


async def _deliver_horoscope(
    bot,
    user_id: int,
    sign: str,
    kind: str,  # 'today' or 'tomorrow'
) -> bool:
    """Generate and send one horoscope message. Returns True on success."""
    from app.intent_router import _handle_horoscope
    from app.utils.text_format import markdown_to_html

    sign_label = _SIGN_LABELS.get(sign, sign.capitalize())
    day_label = "сегодня" if kind == "today" else "завтра"
    query = f"{sign_label} на {day_label}"

    try:
        res = await _handle_horoscope(query)
    except Exception as gen_err:
        logger.error(
            "Horoscope generation error for user=%s sign=%s kind=%s: %s",
            user_id, sign, kind, gen_err, exc_info=True,
        )
        return False

    if not res or not res.text:
        logger.warning("Empty horoscope result for user=%s sign=%s kind=%s", user_id, sign, kind)
        return False

    html_text = markdown_to_html(res.text)
    prefix = "🌅 <b>Ваш гороскоп на сегодня</b>" if kind == "today" else "🌙 <b>Ваш гороскоп на завтра</b>"
    full_text = f"{prefix} ({sign_label}):\n\n{html_text}"

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="horo_settings:edit"),
            InlineKeyboardButton("Отключить", callback_data="horo_settings:delete"),
        ],
    ])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=full_text[:4096],
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return True
    except Exception as send_err:
        logger.error(
            "Failed to send horoscope DM to user=%s: %s",
            user_id, send_err,
        )
        return False


async def check_and_send_horoscopes(context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB job_queue job — fires every minute.

    Checks both 'today' and 'tomorrow' delivery slots against the current UTC
    time, then delivers to all due subscribers.
    """
    from app.repos import settings_repo

    try:
        enabled_raw = await settings_repo.get_global_setting("horoscope_delivery_enabled", "on")
    except Exception as exc:
        logger.error("Horoscope delivery switch lookup failed; skipping delivery tick: %s", exc, exc_info=True)
        return
    if str(enabled_raw).strip().lower() == "off":
        logger.info("Horoscope scheduler skipped: horoscope_delivery_enabled=off")
        return

    now = datetime.now(tz=UTC)
    utc_hour = now.hour
    utc_minute = now.minute

    for kind in ("today", "tomorrow"):
        try:
            due = await get_due_horoscope_subscriptions(utc_hour, utc_minute, kind)
        except Exception as e:
            logger.error("get_due_horoscope_subscriptions failed (kind=%s): %s", kind, e)
            continue

        if not due:
            continue

        logger.info(
            "Horoscope scheduler: %d due '%s' deliveries at UTC %02d:%02d",
            len(due), kind, utc_hour, utc_minute,
        )

        for sub in due:
            user_id: int = sub["user_id"]
            sign: str = sub.get("sign", "aries")

            success = await _deliver_horoscope(context.bot, user_id, sign, kind)
            if success:
                await mark_horoscope_sent(user_id, kind)
                logger.info(
                    "Horoscope '%s' delivered to user=%s sign=%s", kind, user_id, sign
                )
            else:
                logger.warning(
                    "Horoscope '%s' delivery failed for user=%s sign=%s — will retry next minute",
                    kind, user_id, sign,
                )

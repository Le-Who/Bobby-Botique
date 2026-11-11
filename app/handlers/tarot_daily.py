from __future__ import annotations

import hashlib
import html
import logging
from datetime import UTC, date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.tarot_daily import is_preparation_window, prepare_daily_readings, today_reading_date

logger = logging.getLogger(__name__)


def daily_card_for_user(user_id: int, slot_date: date) -> tuple[str, str]:
    """Keep the same card for a subscriber's local day, including manual retry."""
    from app.tarot import iter_daily_card_variants

    variants = iter_daily_card_variants()
    if not variants:
        raise ValueError("tarot_deck_unavailable")
    digest = hashlib.sha256(f"tarot-daily:{user_id}:{slot_date.isoformat()}".encode()).digest()
    card = variants[int.from_bytes(digest[:8], "big") % len(variants)]
    return card["name"], card["orientation"]


async def check_and_send_tarot_readings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admit prepared cards; later ticks catch up interpretations not ready yet."""
    from app.broadcast_jobs import enqueue_scheduled
    from app.errors import is_error_message
    from app.repos.settings_repo import get_global_setting
    from app.repos.tarot_daily_subscriptions import DELIVERY_SETTING_KEY, get_due_for_current_hour
    from app.tarot_daily import get_prepared_daily_readings

    try:
        enabled = await get_global_setting(DELIVERY_SETTING_KEY, "off")
    except Exception as exc:
        logger.warning("Tarot delivery switch unavailable type=%s", type(exc).__name__)
        return
    if str(enabled).strip().lower() == "off":
        return
    now = datetime.now(tz=UTC)
    due = await get_due_for_current_hour(now)
    if not due:
        return
    reading_date = today_reading_date(now)
    prepared = await get_prepared_daily_readings(reading_date=reading_date)
    ready = {
        (reading.card_name, reading.orientation)
        for reading in prepared
        if reading.body_markdown.strip() and not is_error_message(reading.body_markdown)
    }
    recipients = [
        int(sub["user_id"]) for sub in due if daily_card_for_user(int(sub["user_id"]), sub["slot_date"]) in ready
    ]
    if recipients:
        await enqueue_scheduled(channel="tarot", user_ids=recipients, effective_date=reading_date, mode="delivery")


async def deliver_tarot_reading(bot, user_id: int, reading_date: date, *, slot_date: date) -> None:
    """Send only prepared content through the caller's delivery owner."""
    from app.errors import is_error_message
    from app.tarot_daily import get_prepared_daily_reading
    from app.utils.text_format import markdown_to_html, split_text_safe

    card_name, orientation = daily_card_for_user(user_id, slot_date)
    reading = await get_prepared_daily_reading(reading_date=reading_date, card_name=card_name, orientation=orientation)
    if reading is None or not reading.body_markdown.strip() or is_error_message(reading.body_markdown):
        raise ValueError("tarot_reading_unavailable")
    title = html.escape(f"{card_name} ({orientation})")
    text = f"🔮 <b>Карта дня</b>\n<i>Выпала: {title}</i>\n\n{markdown_to_html(reading.body_markdown)}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔕 Отключить Карту дня", callback_data="tarot_daily:unsubscribe")]]
    )
    parts = split_text_safe(text, max_length=3900)
    for index, part in enumerate(parts):
        await bot.send_message(
            chat_id=user_id,
            text=part,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard if index == len(parts) - 1 else None,
        )


async def check_tarot_daily_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_preparation_window():
        return
    try:
        today = today_reading_date()
        for target_date in (today, today + timedelta(days=1)):
            result = await prepare_daily_readings(target_date=target_date)
            logger.info(
                "Tarot daily prep finished date=%s generated=%d skipped=%d failed=%d locked=%s",
                result.target_date,
                result.generated,
                result.skipped,
                result.failed,
                result.locked,
            )
    except Exception as exc:
        logger.warning("Tarot daily prep job failed: %s", exc, exc_info=True)


async def send_tarot_invite(bot, user_id: int) -> bool:
    """Send a daily tarot subscription invite DM to a user.

    Used by the admin manual offer-send flow (/api/admin/broadcast/send-offer).
    Does NOT mark discovery_last_sent_at — the caller in web.py does that.

    Returns True on success, False on Telegram error.
    """
    text = (
        "🔮 <b>Карта дня</b>\n\n"
        "Каждый день — одна карта Таро с персональным раскладом "
        "на основе классической колоды Rider-Waite.\n\n"
        "Включите ежедневную доставку — карта будет приходить "
        "прямо сюда в удобное для вас время."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔮 Подписаться на карту дня", callback_data="tarot_daily:subscribe")],
            [InlineKeyboardButton("❌ Не интересует", callback_data="tarot_daily:dismiss")],
        ]
    )
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True
    except Exception as exc:
        logger.warning("send_tarot_invite failed for user=%s: %s", user_id, exc)
        return False


async def tarot_daily_callback(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries for daily tarot subscription."""
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()

    user_id = update.effective_user.id
    action = query.data.replace("tarot_daily:", "")

    if action == "subscribe":
        from app.repos.tarot_daily_subscriptions import upsert_tarot_subscription

        subscribed = await upsert_tarot_subscription(user_id=user_id, is_subscribed=True)
        if not subscribed:
            await query.edit_message_text(
                "Не удалось включить Карту дня. Ваши настройки не изменились — попробуйте чуть позже.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄 Попробовать снова", callback_data="tarot_daily:subscribe")]]
                ),
            )
            return
        await query.edit_message_text(
            "🔮 Карта дня включена!\n\n"
            "Свежий расклад будет приходить каждый день в 10:00 утра. "
            "Доставку можно отключить кнопкой ниже.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔕 Отключить Карту дня", callback_data="tarot_daily:unsubscribe")]]
            ),
        )
    elif action == "unsubscribe":
        from app.repos.tarot_daily_subscriptions import unsubscribe_tarot

        unsubscribed = await unsubscribe_tarot(user_id)
        if not unsubscribed:
            await query.edit_message_text(
                "Карта дня уже отключена или настройка больше недоступна.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔮 Включить снова", callback_data="tarot_daily:subscribe")]]
                ),
            )
            return
        await query.edit_message_text(
            "🔕 Карта дня отключена. Больше ничего делать не нужно.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔮 Включить снова", callback_data="tarot_daily:subscribe")]]
            ),
        )
    elif action == "dismiss":
        await query.edit_message_text(
            "Хорошо, больше не буду предлагать 🙏",
            reply_markup=InlineKeyboardMarkup([]),
        )

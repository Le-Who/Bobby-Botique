from __future__ import annotations

import logging
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.tarot_daily import is_preparation_window, prepare_daily_readings, today_reading_date

logger = logging.getLogger(__name__)


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
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Подписаться на карту дня", callback_data="tarot_daily:subscribe")],
        [InlineKeyboardButton("❌ Не интересует", callback_data="tarot_daily:dismiss")],
    ])
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

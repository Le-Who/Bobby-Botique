"""Horoscope subscription flow — DM conversation handler.

Entry points:
  1. Deep link: /start subscribe_horoscope_{sign}
     → Landed in DM; we greet and open the setup wizard.
  2. Callback: horo_sub:* — inline button actions within the wizard.

State machine (PTB ConversationHandler):
  CHOOSE_SIGN     → user picks / confirms zodiac sign
  CHOOSE_TIME_TODAY    → pick morning time (or skip)
  CHOOSE_TIME_TOMORROW → pick evening time (or skip)
  CHOOSE_TZ       → if offset unknown, ask; or auto-detect from Telegram
  CONFIRM         → show summary, save, done

Commands:
  /horoscope_settings — re-open the settings panel at any time
  /horoscope_stop     — cancel all deliveries
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.natal.city_catalog import nearest_city_timezone, search_cities
from app.repos.horoscope_subscriptions import (
    delete_horoscope_subscription,
    get_horoscope_subscription,
    upsert_horoscope_subscription,
)

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
CHOOSE_SIGN: Final = "HS_SIGN"
CHOOSE_TIME_TODAY: Final = "HS_TIME_TODAY"
CHOOSE_TIME_TOMORROW: Final = "HS_TIME_TOMORROW"
CHOOSE_TZ: Final = "HS_TZ"
CONFIRM: Final = "HS_CONFIRM"

# ── Zodiac signs ──────────────────────────────────────────────────────────────
SIGNS: dict[str, str] = {
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

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_COMMON_TIMES_MORNING = ["07:00", "08:00", "09:00", "10:00"]
_COMMON_TIMES_EVENING = ["19:00", "20:00", "21:00", "22:00"]


def _sign_keyboard() -> InlineKeyboardMarkup:
    rows = []
    signs = list(SIGNS.items())
    for i in range(0, len(signs), 3):
        chunk = signs[i : i + 3]
        rows.append([InlineKeyboardButton(label, callback_data=f"horo_sign:{key}") for key, label in chunk])
    return InlineKeyboardMarkup(rows)


def _time_keyboard(slot: str, times: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(t, callback_data=f"horo_time_{slot}:{t}") for t in times]]
    rows.append([InlineKeyboardButton("⏭ Пропустить (не получать)", callback_data=f"horo_time_{slot}:skip")])
    return InlineKeyboardMarkup(rows)


def _tz_keyboard() -> InlineKeyboardMarkup:
    offsets = [
        ("UTC−8", -8), ("UTC−5", -5), ("UTC+0", 0), ("UTC+1", 1),
        ("UTC+2", 2), ("UTC+3 (МСК)", 3), ("UTC+4", 4), ("UTC+5", 5),
        ("UTC+6", 6), ("UTC+7", 7), ("UTC+8", 8),
    ]
    rows = []
    for i in range(0, len(offsets), 3):
        chunk = offsets[i : i + 3]
        rows.append([InlineKeyboardButton(label, callback_data=f"horo_tz:{off}") for label, off in chunk])
    return InlineKeyboardMarkup(rows)


def _location_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Отправить локацию", request_location=True)],
            [KeyboardButton("⌨️ Выбрать вручную из списка")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _get_utc_offset_from_tz(tz_name: str) -> int:
    try:
        now = datetime.now(ZoneInfo(tz_name))
        return int(now.utcoffset().total_seconds() / 3600)
    except Exception:
        return 0


def _summary_text(sign: str, time_today: str | None, time_tomorrow: str | None, utc_offset: int) -> str:
    sign_label = SIGNS.get(sign, sign)
    today_str = f"🌅 Утренний (сегодня): <b>{time_today}</b>" if time_today else "🌅 Утренний: <i>отключён</i>"
    tomorrow_str = (
        f"🌙 Вечерний (завтра): <b>{time_tomorrow}</b>" if time_tomorrow else "🌙 Вечерний: <i>отключён</i>"
    )
    tz_sign = "+" if utc_offset >= 0 else ""
    return (
        f"✨ <b>Гороскоп по подписке</b>\n\n"
        f"Знак: {sign_label}\n"
        f"{today_str}\n"
        f"{tomorrow_str}\n"
        f"Часовой пояс: UTC{tz_sign}{utc_offset}\n\n"
        f"Всё верно?"
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Сохранить", callback_data="horo_confirm:yes"),
            InlineKeyboardButton("✏️ Изменить", callback_data="horo_confirm:edit"),
        ]
    ])


# ── Entry: deep link /start subscribe_horoscope_{sign} ───────────────────────

async def start_subscribe_horoscope(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    """Called from /start handler when payload starts with 'subscribe_horoscope_' or via callback."""
    if not update.effective_message:
        return ConversationHandler.END

    payload: str = context.user_data.get("horo_payload", "")
    sign = payload.replace("subscribe_horoscope_", "").lower().strip()
    
    if update.callback_query:
        await update.callback_query.answer()
        if update.callback_query.data == "start_horoscope":
            sign = ""

    if sign not in SIGNS:
        sign = ""  # Ask user to pick

    context.user_data["horo_sign"] = sign

    if sign:
        text = (
            f"✨ <b>Подписка на гороскоп</b>\n\n"
            f"Вы хотите получать гороскоп для знака <b>{SIGNS[sign]}</b>?\n\n"
            f"Подтвердите или выберите другой знак:"
        )
    else:
        text = "✨ <b>Подписка на гороскоп</b>\n\nВыберите ваш знак зодиака:"
        
    if update.callback_query:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode="HTML",
            reply_markup=_sign_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_sign_keyboard(),
        )
    return CHOOSE_SIGN


# ── State: CHOOSE_SIGN ────────────────────────────────────────────────────────

async def on_sign_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query:
        return CHOOSE_SIGN
    await query.answer()

    sign = query.data.replace("horo_sign:", "")
    context.user_data["horo_sign"] = sign

    await query.edit_message_text(
        f"<b>{SIGNS[sign]}</b> — отлично!\n\n"
        f"🌅 В какое время отправлять <b>гороскоп на сегодня</b> (утром)?\n"
        f"Укажите или выберите, либо нажмите «Пропустить»:",
        parse_mode="HTML",
        reply_markup=_time_keyboard("today", _COMMON_TIMES_MORNING),
    )
    return CHOOSE_TIME_TODAY


# ── State: CHOOSE_TIME_TODAY ──────────────────────────────────────────────────

async def on_time_today_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query:
        return CHOOSE_TIME_TODAY
    await query.answer()

    value = query.data.replace("horo_time_today:", "")
    context.user_data["horo_time_today"] = None if value == "skip" else value

    await query.edit_message_text(
        "🌙 В какое время отправлять <b>гороскоп на завтра</b> (вечером)?\n"
        "Укажите или выберите, либо нажмите «Пропустить»:",
        parse_mode="HTML",
        reply_markup=_time_keyboard("tomorrow", _COMMON_TIMES_EVENING),
    )
    return CHOOSE_TIME_TOMORROW


async def on_time_today_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    """Handle free-text time input like '09:30'."""
    if not update.message or not update.message.text:
        return CHOOSE_TIME_TODAY
    raw = update.message.text.strip()
    if not _TIME_RE.match(raw):
        await update.message.reply_text(
            "❌ Неверный формат. Введите время в формате <code>ЧЧ:ММ</code> (например <code>09:00</code>).",
            parse_mode="HTML",
        )
        return CHOOSE_TIME_TODAY

    context.user_data["horo_time_today"] = raw
    await update.message.reply_text(
        "🌙 В какое время отправлять <b>гороскоп на завтра</b> (вечером)?\n"
        "Укажите или выберите, либо нажмите «Пропустить»:",
        parse_mode="HTML",
        reply_markup=_time_keyboard("tomorrow", _COMMON_TIMES_EVENING),
    )
    return CHOOSE_TIME_TOMORROW


# ── State: CHOOSE_TIME_TOMORROW ───────────────────────────────────────────────

async def on_time_tomorrow_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query:
        return CHOOSE_TIME_TOMORROW
    await query.answer()

    value = query.data.replace("horo_time_tomorrow:", "")
    context.user_data["horo_time_tomorrow"] = None if value == "skip" else value

    text = (
        "🌍 <b>Настройка часового пояса</b>\n\n"
        "Напишите <b>название вашего города</b> (например, Москва, Киев) "
        "или нажмите кнопку «Отправить локацию».\n\n"
        "Бот автоматически рассчитает смещение UTC."
    )
    await query.message.delete()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        reply_markup=_location_request_keyboard(),
        parse_mode="HTML",
    )
    return CHOOSE_TZ


async def on_time_tomorrow_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    if not update.message or not update.message.text:
        return CHOOSE_TIME_TOMORROW
    raw = update.message.text.strip()
    if not _TIME_RE.match(raw):
        await update.message.reply_text(
            "❌ Неверный формат. Введите время в формате <code>ЧЧ:ММ</code> (например <code>20:00</code>).",
            parse_mode="HTML",
        )
        return CHOOSE_TIME_TOMORROW

    context.user_data["horo_time_tomorrow"] = raw
    text = (
        "🌍 <b>Настройка часового пояса</b>\n\n"
        "Напишите <b>название вашего города</b> (например, Москва, Киев) "
        "или нажмите кнопку «Отправить локацию».\n\n"
        "Бот автоматически рассчитает смещение UTC."
    )
    await update.message.reply_text(
        text,
        reply_markup=_location_request_keyboard(),
        parse_mode="HTML",
    )
    return CHOOSE_TZ


# ── State: CHOOSE_TZ ──────────────────────────────────────────────────────────

async def _show_summary(message, context: ContextTypes.DEFAULT_TYPE) -> str:
    sign = context.user_data.get("horo_sign", "aries")
    time_today = context.user_data.get("horo_time_today")
    time_tomorrow = context.user_data.get("horo_time_tomorrow")
    utc_offset = context.user_data.get("horo_utc_offset", 3)

    dummy = await message.reply_text("⏳", reply_markup=ReplyKeyboardRemove())
    await dummy.delete()

    await message.reply_text(
        _summary_text(sign, time_today, time_tomorrow, utc_offset),
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(),
    )
    return CONFIRM


async def on_tz_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    if not update.message or not update.message.location:
        return CHOOSE_TZ
    lat = update.message.location.latitude
    lon = update.message.location.longitude
    tz_name = nearest_city_timezone(lat, lon)
    if not tz_name:
        await update.message.reply_text("Не удалось определить часовой пояс. Выберите из списка:", reply_markup=_tz_keyboard())
        return CHOOSE_TZ
    utc_offset = _get_utc_offset_from_tz(tz_name)
    context.user_data["horo_utc_offset"] = utc_offset
    return await _show_summary(update.message, context)


async def on_tz_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    if not update.message or not update.message.text:
        return CHOOSE_TZ
    query = update.message.text.strip()
    cities = search_cities(query)
    if not cities:
        await update.message.reply_text("Город не найден. Попробуйте написать по-другому или выберите из списка:", reply_markup=_tz_keyboard())
        return CHOOSE_TZ
    city = cities[0]
    utc_offset = _get_utc_offset_from_tz(city.timezone)
    context.user_data["horo_utc_offset"] = utc_offset
    await update.message.reply_text(f"Определен город: <b>{city.name}</b>", parse_mode="HTML")
    return await _show_summary(update.message, context)


async def on_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    if not update.message:
        return CHOOSE_TZ
    await update.message.reply_text("🌍 Укажите ваш часовой пояс (UTC-смещение):", reply_markup=_tz_keyboard())
    return CHOOSE_TZ


async def on_tz_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query:
        return CHOOSE_TZ
    await query.answer()

    utc_offset = int(query.data.replace("horo_tz:", ""))
    context.user_data["horo_utc_offset"] = utc_offset

    sign = context.user_data.get("horo_sign", "aries")
    time_today = context.user_data.get("horo_time_today")
    time_tomorrow = context.user_data.get("horo_time_tomorrow")

    await query.edit_message_text(
        _summary_text(sign, time_today, time_tomorrow, utc_offset),
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(),
    )
    return CONFIRM


# ── State: CONFIRM ────────────────────────────────────────────────────────────

async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query:
        return CONFIRM
    await query.answer()

    action = query.data.replace("horo_confirm:", "")
    if action == "edit":
        # Restart from sign selection
        await query.edit_message_text(
            "✨ Выберите ваш знак зодиака:",
            reply_markup=_sign_keyboard(),
        )
        return CHOOSE_SIGN

    # Save subscription
    user_id = update.effective_user.id if update.effective_user else None
    if not user_id:
        await query.edit_message_text("❌ Не удалось определить пользователя.")
        return ConversationHandler.END

    sign = context.user_data.get("horo_sign", "aries")
    time_today = context.user_data.get("horo_time_today")
    time_tomorrow = context.user_data.get("horo_time_tomorrow")
    utc_offset = context.user_data.get("horo_utc_offset", 3)

    ok = await upsert_horoscope_subscription(
        user_id=user_id,
        sign=sign,
        time_today=time_today,
        time_tomorrow=time_tomorrow,
        utc_offset=utc_offset,
        is_active=True,
    )

    if ok:
        sign_label = SIGNS.get(sign, sign)
        parts = []
        if time_today:
            parts.append(f"🌅 Утренний гороскоп (сегодня): <b>{time_today}</b>")
        if time_tomorrow:
            parts.append(f"🌙 Вечерний гороскоп (завтра): <b>{time_tomorrow}</b>")
        delivery_lines = "\n".join(parts) if parts else "⚠️ Оба слота отключены — доставки не будет."

        await query.edit_message_text(
            f"✅ <b>Подписка сохранена!</b>\n\n"
            f"Знак: {sign_label}\n"
            f"{delivery_lines}\n\n"
            f"Чтобы изменить — /horoscope_settings\n"
            f"Чтобы отключить — /horoscope_stop",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([]),
        )
    else:
        await query.edit_message_text("❌ Ошибка при сохранении подписки. Попробуйте позже.")

    # Clean up conversation state
    for key in ("horo_sign", "horo_time_today", "horo_time_tomorrow", "horo_utc_offset", "horo_payload"):
        context.user_data.pop(key, None)

    return ConversationHandler.END


# ── /horoscope_settings command ───────────────────────────────────────────────

async def horoscope_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    """Re-open the subscription wizard from any state."""
    if not update.message or not update.effective_user:
        return ConversationHandler.END

    user_id = update.effective_user.id
    sub = await get_horoscope_subscription(user_id)

    if sub:
        sign = sub.get("sign", "aries")
        time_today = sub.get("time_today")
        if hasattr(time_today, "strftime"):
            time_today = time_today.strftime("%H:%M")
        time_tomorrow = sub.get("time_tomorrow")
        if hasattr(time_tomorrow, "strftime"):
            time_tomorrow = time_tomorrow.strftime("%H:%M")
        utc_offset = sub.get("utc_offset", 3)
        is_active = sub.get("is_active", True)

        status = "✅ Активна" if is_active else "⏸ Приостановлена"
        sign_label = SIGNS.get(sign, sign)
        today_str = f"🌅 Утро: <b>{time_today}</b>" if time_today else "🌅 Утро: <i>отключено</i>"
        tomorrow_str = f"🌙 Вечер: <b>{time_tomorrow}</b>" if time_tomorrow else "🌙 Вечер: <i>отключено</i>"
        tz_sign = "+" if utc_offset >= 0 else ""

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить подписку", callback_data="horo_settings:edit")],
            [InlineKeyboardButton(
                "⏸ Приостановить" if is_active else "▶️ Возобновить",
                callback_data="horo_settings:toggle",
            )],
            [InlineKeyboardButton("🗑 Удалить подписку", callback_data="horo_settings:delete")],
        ])

        await update.message.reply_text(
            f"<b>Ваша подписка на гороскоп</b>\n\n"
            f"Знак: {sign_label}\n"
            f"{today_str}\n"
            f"{tomorrow_str}\n"
            f"Часовой пояс: UTC{tz_sign}{utc_offset}\n"
            f"Статус: {status}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            "У вас нет активной подписки на гороскоп.\n\n"
            "Вы можете оформить её прямо сейчас, чтобы получать ежедневные прогнозы в удобное время.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✨ Оформить подписку", callback_data="start_horoscope")]
            ])
        )
    return ConversationHandler.END


# ── /horoscope_settings callbacks ────────────────────────────────────────────

async def horoscope_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    query = update.callback_query
    if not query or not update.effective_user:
        return ConversationHandler.END
    await query.answer()

    user_id = update.effective_user.id
    action = query.data.replace("horo_settings:", "")

    if action == "start":
        # Triggered from admin-sent discovery invite — open sign selection wizard
        await query.edit_message_text(
            "✨ Выберите ваш знак зодиака:",
            reply_markup=_sign_keyboard(),
        )
        return CHOOSE_SIGN

    if action == "edit":
        sub = await get_horoscope_subscription(user_id)
        if sub:
            context.user_data["horo_sign"] = sub.get("sign", "aries")
        await query.edit_message_text(
            "✨ Выберите ваш знак зодиака:",
            reply_markup=_sign_keyboard(),
        )
        return CHOOSE_SIGN

    if action == "dismiss":
        # User declined the invite — quietly close the keyboard
        await query.edit_message_text(
            "Хорошо, больше не буду предлагать 🙏",
            reply_markup=InlineKeyboardMarkup([]),
        )
        return ConversationHandler.END

    if action == "toggle":
        sub = await get_horoscope_subscription(user_id)
        if sub:
            new_active = not sub.get("is_active", True)
            await upsert_horoscope_subscription(user_id=user_id, is_active=new_active)
            status = "▶️ Возобновлена" if new_active else "⏸ Приостановлена"
            await query.edit_message_text(f"Подписка {status}.", reply_markup=InlineKeyboardMarkup([]))
        return ConversationHandler.END

    if action == "delete":
        await delete_horoscope_subscription(user_id)
        await query.edit_message_text("🗑 Подписка удалена.", reply_markup=InlineKeyboardMarkup([]))
        return ConversationHandler.END

    return ConversationHandler.END


# ── /horoscope_stop command ───────────────────────────────────────────────────

async def horoscope_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | str:
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    user_id = update.effective_user.id
    await delete_horoscope_subscription(user_id)
    await update.message.reply_text("🗑 Подписка на гороскоп отменена.")
    return ConversationHandler.END


# ── Build ConversationHandler ─────────────────────────────────────────────────

import re

HOROSCOPE_INTENT_RE = re.compile(r"^\s*(?:гороскоп|настройка гороскопа|подписка на гороскоп)\s*[?!.]*$", re.IGNORECASE)

def build_horoscope_subscription_handler() -> ConversationHandler:
    """Return a ConversationHandler for the horoscope subscription wizard.

    The conversation is entered via:
      - start_subscribe_horoscope() called from the /start deep link handler
      - /horoscope_settings command (opens existing sub settings)
      - text intent "гороскоп"

    It also registers standalone /horoscope_stop and horo_settings:* callbacks.
    """
    return ConversationHandler(
        entry_points=[
            # Deep link entry — called programmatically from start_command
            CommandHandler("horoscope_settings", horoscope_settings_command),
            MessageHandler(filters.TEXT & filters.Regex(HOROSCOPE_INTENT_RE), start_subscribe_horoscope),
            CallbackQueryHandler(start_subscribe_horoscope, pattern="^start_horoscope$"),
        ],
        states={
            CHOOSE_SIGN: [CallbackQueryHandler(on_sign_chosen, pattern="^horo_sign:")],
            CHOOSE_TIME_TODAY: [
                CallbackQueryHandler(on_time_today_btn, pattern="^horo_time_today:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_today_text),
            ],
            CHOOSE_TIME_TOMORROW: [
                CallbackQueryHandler(on_time_tomorrow_btn, pattern="^horo_time_tomorrow:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_time_tomorrow_text),
            ],
            CHOOSE_TZ: [
                MessageHandler(filters.LOCATION, on_tz_location),
                MessageHandler(filters.Regex("^⌨️ Выбрать вручную из списка$"), on_tz_manual),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_tz_text),
                CallbackQueryHandler(on_tz_chosen, pattern="^horo_tz:"),
            ],
            CONFIRM: [CallbackQueryHandler(on_confirm, pattern="^horo_confirm:")],
        },
        fallbacks=[
            CommandHandler("horoscope_stop", horoscope_stop_command),
        ],
        allow_reentry=True,
        per_message=False,
        name="horoscope_subscription",
        persistent=False,
    )


async def send_horoscope_invite(bot, user_id: int) -> bool:
    """Send a horoscope subscription invite DM to a user.

    Used by the admin manual offer-send flow (/api/admin/broadcast/send-offer).
    Mirrors the pattern of send_discovery_intro in daily_crocodile.py.
    Does NOT mark discovery_last_sent_at — the caller in web.py does that.

    Returns True on success, False on Telegram error.
    """
    from telegram.constants import ParseMode

    text = (
        "⭐ <b>Гороскоп по подписке</b>\n\n"
        "Получайте персональный гороскоп каждое утро и/или вечер — "
        "прямо в этот чат, точно в выбранное время.\n\n"
        "Выберите свой знак зодиака и настройте расписание."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Настроить подписку", callback_data="horo_settings:start")],
        [InlineKeyboardButton("❌ Не интересует", callback_data="horo_settings:dismiss")],
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
        logger.warning("send_horoscope_invite failed for user=%s: %s", user_id, exc)
        return False


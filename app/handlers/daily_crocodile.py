from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.repos import crocodile_daily as repo
from app.repos.settings_repo import get_global_setting
from app.utils.decorators import authorized_only, safe_handler

logger = logging.getLogger(__name__)

_TIME_CHOICES = (9, 13, 19, 21)
_PLACEHOLDER_KEY = "daily_croc_placeholder_file_id"

# Simple in-process cache so we don't hit the DB on every single delivery.
_placeholder_cache: str = ""
_placeholder_cache_ts: float = 0.0
_PLACEHOLDER_TTL = 60.0  # seconds


async def _get_placeholder_file_id() -> str:
    import time

    global _placeholder_cache, _placeholder_cache_ts  # noqa: PLW0603
    now = time.monotonic()
    if now - _placeholder_cache_ts < _PLACEHOLDER_TTL:
        return _placeholder_cache
    val = await get_global_setting(_PLACEHOLDER_KEY, "")
    _placeholder_cache = str(val or "")
    _placeholder_cache_ts = now
    return _placeholder_cache



def _webapp_base() -> str:
    from app.config import settings

    base = getattr(settings, "WEBAPP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    return webhook_url.split("/webhook")[0].rstrip("/")


def daily_game_url() -> str:
    base = _webapp_base()
    return f"{base}/webapp/game?mode=daily" if base else ""


def _play_button(label: str = "Играть") -> InlineKeyboardButton:
    """Build the "Play" button for the daily Crocodile prompt.

    Uses a ``t.me`` deep link (``https://t.me/BOT/APP?startapp=daily``)
    so Telegram opens the page inside its Mini App viewer and injects
    ``initData`` — required by the WS auth layer.

    A plain ``url=`` would open a regular browser tab where
    ``tg.initData`` is empty → WS close 4003 "initData required".

    NOTE: ``web_app=WebAppInfo(...)`` is NOT used because Telegram
    rejects that button type when the message is later edited via a
    CallbackQuery that carries ``inline_message_id``
    (error ``Button_type_invalid``).
    """
    from app.bot_instance import get_bot
    from app.config import settings

    miniapp_short = getattr(settings, "MINIAPP_SHORT_NAME", "").strip()
    bot = get_bot()
    bot_username = getattr(bot, "username", "") if bot else ""
    if miniapp_short and bot_username:
        url = f"https://t.me/{bot_username}/{miniapp_short}?startapp=daily"
    else:
        url = daily_game_url() or "https://t.me"
    return InlineKeyboardButton(label, url=url)


def daily_intro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_play_button("Открыть daily")],
            [InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")],
            [InlineKeyboardButton("Не напоминать 2 недели", callback_data="dailycroc:snooze")],
        ]
    )


def daily_play_keyboard(*, include_subscribe: bool = True) -> InlineKeyboardMarkup:
    rows = [[_play_button("Открыть daily")]]
    if include_subscribe:
        rows.append([InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")])
    return InlineKeyboardMarkup(rows)


def subscribe_time_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"dailycroc:time:{hour}")]
            for hour in _TIME_CHOICES
        ]
    )


async def is_daily_delivery_enabled() -> bool:
    value = await get_global_setting(repo.DAILY_DELIVERY_SETTING_KEY, "on")
    return value.strip().lower() != "off"


async def send_discovery_intro(bot, user_id: int) -> bool:
    text = (
        "🐊 <b>Крокодил дня</b>\n\n"
        "Каждый день два независимых режима: <b>Easy</b> и <b>Hard</b>. "
        "У каждого свои очки, лидерборд и completion state.\n\n"
        "Можно сыграть сейчас или включить ежедневное напоминание."
    )
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=daily_intro_keyboard(),
    )
    await repo.mark_discovery_sent(user_id)
    return True


async def send_daily_prompt(bot, user_id: int, puzzle_date) -> bool:
    caption = (
        f"🐊 <b>Крокодил дня</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
        "Сегодня уже готовы <b>Easy</b> и <b>Hard</b>. Оба режима можно пройти отдельно."
    )
    keyboard = daily_play_keyboard(include_subscribe=False)
    placeholder_file_id = (await _get_placeholder_file_id()).strip()
    if placeholder_file_id:
        try:
            msg = await bot.send_photo(
                chat_id=user_id,
                photo=placeholder_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            await repo.register_prompt_message(
                user_id=user_id,
                puzzle_date=puzzle_date,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
        except Exception as exc:
            logger.warning("daily prompt photo failed user=%s: %s — falling back to text", user_id, exc)
            await bot.send_message(
                chat_id=user_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
    else:
        await bot.send_message(
            chat_id=user_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    pref = await repo.get_preference(user_id)
    await repo.mark_daily_sent(
        user_id,
        puzzle_date,
        timezone=(pref or {}).get("timezone"),
    )
    return True


@authorized_only
@safe_handler("Не удалось открыть Крокодил дня")
async def dailycroc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    await repo.record_player_activity(user_id, event="daily_played")
    pref = await repo.get_preference(user_id)
    is_subscribed = bool(pref and pref.get("is_subscribed"))
    # Mark delivery so the scheduler doesn't send a duplicate today.
    if is_subscribed:
        now = datetime.now(tz=UTC)
        today = repo.today_puzzle_date(now)
        if not repo.was_daily_delivered_today(pref, puzzle_date=today, now=now):
            await repo.mark_daily_sent(
                user_id,
                today,
                now=now,
                timezone=(pref or {}).get("timezone"),
            )
    text = (
        "🐊 <b>Крокодил дня</b>\n\n"
        "На сегодня доступны <b>Easy</b> и <b>Hard</b>. "
        "У каждого режима свои очки, completion и лидерборд."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=daily_play_keyboard(include_subscribe=not is_subscribed),
    )


async def daily_subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    await repo.upsert_preference(update.effective_user.id)
    await query.edit_message_text(
        "Когда присылать ежедневного Крокодила?\n\nВыбери удобное местное время:",
        reply_markup=subscribe_time_keyboard(),
    )


async def daily_time_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    try:
        hour = int(query.data.rsplit(":", 1)[-1])
    except ValueError:
        await query.answer("Некорректное время", show_alert=True)
        return
    pref = await repo.upsert_preference(
        update.effective_user.id,
        is_subscribed=True,
        preferred_local_hour=hour,
    )
    await query.answer("Подписка включена")
    tz = pref.get("timezone") or repo.DEFAULT_TIMEZONE
    await query.edit_message_text(
        f"✅ Готово. Буду присылать Крокодила дня примерно в {hour:02d}:00.\n"
        f"Часовой пояс определю автоматически; сейчас используется {tz}.",
        reply_markup=daily_play_keyboard(include_subscribe=False),
    )


async def daily_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    until = await repo.snooze_discovery(update.effective_user.id)
    await query.answer("Не буду напоминать 2 недели")
    await query.edit_message_text(
        f"👌 Не буду напоминать до {until.astimezone().strftime('%d.%m.%Y')}.\n\n"
        "Если захочешь сыграть раньше, команда /dailycroc всегда доступна.",
        reply_markup=daily_play_keyboard(include_subscribe=False),
    )


async def daily_unsubscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await repo.unsubscribe(update.effective_user.id)
    await query.answer("Подписка отменена")
    try:
        await query.edit_message_reply_markup(
            reply_markup=daily_play_keyboard(include_subscribe=True)
        )
    except Exception:
        pass


async def check_daily_crocodile_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.games.crocodile_daily import active_daily_difficulties, ensure_prepared_puzzles

    now = datetime.now(tz=UTC)
    prepared = await ensure_prepared_puzzles(context.bot, now=now)
    today = repo.today_puzzle_date(now)
    required = await active_daily_difficulties()
    today_puzzles = {item.difficulty: item for item in prepared if item.puzzle_date == today}
    if not today_puzzles:
        today_puzzles = await repo.get_puzzles_for_date(today)
    missing = [difficulty for difficulty in required if difficulty not in today_puzzles]
    if missing:
        logger.warning("daily Crocodile scheduler: puzzle missing for %s difficulties=%s", today, ",".join(missing))
        return
    not_ready = [difficulty for difficulty in required if not repo.is_puzzle_fully_prepared(today_puzzles[difficulty])]
    if not_ready:
        logger.warning("daily Crocodile scheduler: puzzle %s not fully prepared for difficulties=%s; skipping sends", today, ",".join(not_ready))
        return
    if not await is_daily_delivery_enabled():
        logger.info("daily Crocodile delivery disabled by admin switch; pre-generation kept running")
        return

    due_delivery = await repo.get_due_deliveries(puzzle_date=today, now=now)
    for item in due_delivery:
        try:
            await send_daily_prompt(context.bot, int(item["user_id"]), today)
        except Exception as exc:
            logger.warning("daily Crocodile delivery failed user=%s: %s", item.get("user_id"), exc)

    discovery = await repo.get_discovery_candidates(now=now)
    for item in discovery:
        try:
            await send_discovery_intro(context.bot, int(item["user_id"]))
        except Exception as exc:
            logger.warning("daily Crocodile discovery failed user=%s: %s", item.get("user_id"), exc)

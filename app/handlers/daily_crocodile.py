from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.repos import crocodile_daily as repo
from app.repos.settings_repo import get_global_setting
from app.utils.decorators import authorized_only, safe_handler

logger = logging.getLogger(__name__)

_TIME_CHOICES = (9, 13, 19, 21)


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
    url = daily_game_url()
    if url.startswith("https://"):
        return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    return InlineKeyboardButton(label, url=url or "https://t.me")


def daily_intro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_play_button("Играть")],
            [InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")],
            [InlineKeyboardButton("Не напоминать 2 недели", callback_data="dailycroc:snooze")],
        ]
    )


def daily_play_keyboard(*, include_subscribe: bool = True) -> InlineKeyboardMarkup:
    rows = [[_play_button("Играть")]]
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
        "Каждый день одно слово для всех: угадываешь за 6 попыток, получаешь очки, "
        "поднимаешься в лидерборде и держишь серию побед.\n\n"
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
    text = (
        f"🐊 <b>Крокодил дня</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
        "Сегодняшнее слово уже ждёт. У тебя 6 попыток."
    )
    await bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=daily_play_keyboard(include_subscribe=False),
    )
    await repo.mark_daily_sent(user_id, puzzle_date)
    return True


@authorized_only
@safe_handler("Не удалось открыть Крокодил дня")
async def dailycroc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    await repo.record_player_activity(user_id, event="daily_played")
    pref = await repo.get_preference(user_id)
    include_subscribe = not bool(pref and pref.get("is_subscribed"))
    text = (
        "🐊 <b>Крокодил дня</b>\n\n"
        "Одно слово для всех на сегодня. 6 попыток, очки, серия и живой лидерборд."
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=daily_play_keyboard(include_subscribe=include_subscribe),
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


async def check_daily_crocodile_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.games.crocodile_daily import ensure_prepared_puzzles

    now = datetime.now(tz=UTC)
    prepared = await ensure_prepared_puzzles(context.bot, now=now)
    today = repo.today_puzzle_date(now)
    puzzle = next((item for item in prepared if item.puzzle_date == today), None)
    if puzzle is None:
        puzzle = await repo.get_puzzle(today)
    if puzzle is None:
        logger.warning("daily Crocodile scheduler: puzzle missing for %s after pre-generation", today)
        return
    if not repo.is_puzzle_fully_prepared(puzzle):
        logger.warning("daily Crocodile scheduler: puzzle %s not fully prepared yet; skipping sends", puzzle.puzzle_date)
        return
    if not await is_daily_delivery_enabled():
        logger.info("daily Crocodile delivery disabled by admin switch; pre-generation kept running")
        return

    due_delivery = await repo.get_due_deliveries(puzzle_date=puzzle.puzzle_date, now=now)
    for item in due_delivery:
        try:
            await send_daily_prompt(context.bot, int(item["user_id"]), puzzle.puzzle_date)
        except Exception as exc:
            logger.warning("daily Crocodile delivery failed user=%s: %s", item.get("user_id"), exc)

    discovery = await repo.get_discovery_candidates(now=now)
    for item in discovery:
        try:
            await send_discovery_intro(context.bot, int(item["user_id"]))
        except Exception as exc:
            logger.warning("daily Crocodile discovery failed user=%s: %s", item.get("user_id"), exc)

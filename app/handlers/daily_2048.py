from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from app.config import settings
from app.games.daily_2048_telegram import _get_cover_photo, _remember_cover_file_id
from app.repos import crocodile_daily as daily_delivery_repo
from app.repos import daily_2048 as repo
from app.utils.decorators import safe_handler

logger = logging.getLogger(__name__)


def _webapp_base() -> str:
    base = getattr(settings, "WEBAPP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    return webhook_url.split("/webhook")[0].rstrip("/")


def daily2048_game_url() -> str:
    base = _webapp_base()
    return f"{base}/webapp/daily2048" if base else ""


def daily2048_play_button(label: str = "Открыть 2048") -> InlineKeyboardButton:
    from app.bot_instance import get_bot

    miniapp_short = getattr(settings, "MINIAPP_SHORT_NAME", "").strip()
    bot = get_bot()
    bot_username = getattr(bot, "username", "") if bot else ""
    if miniapp_short and bot_username:
        url = f"https://t.me/{bot_username}/{miniapp_short}?startapp=daily2048"
    else:
        url = daily2048_game_url() or "https://t.me"
    return InlineKeyboardButton(label, url=url)


def daily2048_entry_keyboard(*, include_subscribe: bool = True) -> InlineKeyboardMarkup:
    rows = [[daily2048_play_button("Открыть 2048")]]
    if include_subscribe:
        rows.append([InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")])
    return InlineKeyboardMarkup(rows)


def _entry_caption(puzzle_date) -> str:
    return (
        f"🎲 <b>Daily 2048 Sprint</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
        "Цель дня видна прямо в игре: собери нужный кубик или стоимость как можно быстрее. "
        "Первое решение фиксируется в рекорды дня; дальше можно играть без записи в таблицу."
    )


async def send_daily2048_entry(
    bot,
    user_id: int,
    puzzle_date,
    *,
    include_subscribe: bool = False,
    reply_to_message_id: int | None = None,
    mark_delivered: bool = True,
) -> None:
    caption = _entry_caption(puzzle_date)
    keyboard = daily2048_entry_keyboard(include_subscribe=include_subscribe)
    send_kwargs = {
        "chat_id": user_id,
        "photo": await _get_cover_photo(),
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": keyboard,
    }
    if reply_to_message_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_message_id
    try:
        message = await bot.send_photo(**send_kwargs)
        await _remember_cover_file_id(message)
        await repo.register_prompt_message(
            user_id=user_id,
            puzzle_date=puzzle_date,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
    except (OSError, TelegramError) as exc:
        logger.warning("daily 2048 cover prompt failed user=%s: %s", user_id, exc)
        fallback_kwargs = {
            "chat_id": user_id,
            "text": caption,
            "parse_mode": ParseMode.HTML,
            "reply_markup": keyboard,
        }
        if reply_to_message_id is not None:
            fallback_kwargs["reply_to_message_id"] = reply_to_message_id
        message = await bot.send_message(**fallback_kwargs)
        await repo.register_prompt_message(
            user_id=user_id,
            puzzle_date=puzzle_date,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
    if mark_delivered:
        pref = await daily_delivery_repo.get_preference(user_id)
        await daily_delivery_repo.mark_daily_sent(
            user_id,
            puzzle_date,
            timezone=(pref or {}).get("timezone"),
        )


async def send_discovery_intro(bot, user_id: int) -> bool:
    text = (
        "🎲 <b>Новая daily-игра: 2048 Sprint</b>\n\n"
        "Каждый день короткий 2048-челлендж на скорость. "
        "Собери цель дня за минимальное время и попади в таблицу месяца."
    )
    keyboard = daily2048_entry_keyboard(include_subscribe=True)
    try:
        message = await bot.send_photo(
            chat_id=user_id,
            photo=await _get_cover_photo(),
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        await _remember_cover_file_id(message)
    except (OSError, TelegramError) as exc:
        logger.warning("daily 2048 discovery cover failed user=%s: %s", user_id, exc)
        await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    await daily_delivery_repo.mark_discovery_sent(user_id)
    return True


@safe_handler("Не удалось открыть Daily 2048")
async def daily2048_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    today = repo.today_puzzle_date(datetime.now(tz=UTC))
    await repo.ensure_puzzle(today)
    pref = await daily_delivery_repo.get_preference(user_id)
    is_subscribed = bool(pref and pref.get("is_subscribed"))
    await send_daily2048_entry(
        context.bot,
        user_id,
        today,
        include_subscribe=not is_subscribed,
        reply_to_message_id=update.message.message_id,
        mark_delivered=False,
    )


async def check_daily_2048_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.games.daily_2048 import ensure_prepared_puzzles
    from app.handlers.daily_crocodile import is_daily_delivery_enabled

    now = datetime.now(tz=UTC)
    try:
        await ensure_prepared_puzzles(now=now)
    except Exception as exc:
        logger.error("daily 2048: ensure_prepared_puzzles failed: %s", exc, exc_info=True)
        return

    today = repo.today_puzzle_date(now)
    if not await is_daily_delivery_enabled():
        logger.info("daily 2048 delivery disabled by daily delivery switch")
        return

    due_delivery = await daily_delivery_repo.get_due_deliveries(puzzle_date=today, now=now)
    if due_delivery:
        sem = asyncio.Semaphore(10)

        async def _bounded_send(item):
            async with sem:
                try:
                    await send_daily2048_entry(context.bot, int(item["user_id"]), today)
                except Exception as exc:
                    logger.warning("daily 2048 delivery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_send(item) for item in due_delivery])

    discovery = await daily_delivery_repo.get_discovery_candidates(now=now)
    if discovery:
        sem_disc = asyncio.Semaphore(10)

        async def _bounded_discovery(item):
            async with sem_disc:
                try:
                    await send_discovery_intro(context.bot, int(item["user_id"]))
                except Exception as exc:
                    logger.warning("daily 2048 discovery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_discovery(item) for item in discovery])


async def monthly_champions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    if not query or not query.data:
        return
    raw_month = query.data.rsplit(":", 1)[-1]
    try:
        year_s, month_s = raw_month.split("-", 1)
        year = int(year_s)
        month = int(month_s)
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        await query.answer("Некорректный месяц", show_alert=True)
        return

    champions = await repo.get_monthly_champions(year, month)
    await query.answer("Загружаю лучших за месяц")
    if not query.message:
        return
    lines = [f"🏆 <b>Лучшие игроки {month:02d}.{year}</b>", ""]
    if not champions:
        lines.append("Пока нет завершённых daily-результатов.")
    else:
        for item in champions:
            puzzle_date = item["puzzle_date"]
            name = html.escape((item.get("display_name") or "").strip())
            lines.append(
                f"{puzzle_date.strftime('%d.%m')} — <b>{name}</b>: "
                f"{int(item['final_score'])} очков · {int(item['moves'])} ходов"
            )
    await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

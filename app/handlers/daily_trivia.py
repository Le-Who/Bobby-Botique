from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config import settings
from app.games.daily_trivia import ensure_prepared_puzzles, prepare_daily_puzzle
from app.repos import crocodile_daily as daily_delivery_repo
from app.repos import daily_trivia as trivia_repo
from app.repos.crocodile_daily import today_puzzle_date
from app.utils.decorators import safe_handler

logger = logging.getLogger(__name__)


def _webapp_base() -> str:
    base = getattr(settings, "WEBAPP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    return webhook_url.split("/webhook")[0].rstrip("/")


def daily_trivia_game_url() -> str:
    base = _webapp_base()
    return f"{base}/webapp/dailytrivia" if base else ""


def daily_trivia_play_button(label: str = "🧠 Начать Викторину") -> InlineKeyboardButton:
    from app.bot_instance import get_bot

    miniapp_short = getattr(settings, "MINIAPP_SHORT_NAME", "").strip()
    bot = get_bot()
    bot_username = getattr(bot, "username", "") if bot else ""
    if miniapp_short and bot_username:
        url = f"https://t.me/{bot_username}/{miniapp_short}?startapp=dailytrivia"
    else:
        url = daily_trivia_game_url() or "https://t.me"
    return InlineKeyboardButton(label, url=url)


def daily_trivia_keyboard(*, include_subscribe: bool = False) -> InlineKeyboardMarkup:
    rows = [[daily_trivia_play_button()]]
    if include_subscribe:
        rows.append([InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")])
    return InlineKeyboardMarkup(rows)


def _entry_caption(puzzle_date) -> str:
    return (
        f"🧠 <b>Daily Trivia</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
        "Вас ждут 5 уникальных интеллектуальных вопросов на самые разные темы! "
        "Узнавайте новые познавательные факты после каждого ответа и зарабатывайте максимум очков за точность и скорость."
    )


async def send_daily_trivia_entry(
    bot,
    user_id: int,
    puzzle_date,
    *,
    include_subscribe: bool = False,
    reply_to_message_id: int | None = None,
    mark_delivered: bool = True,
) -> None:
    caption = _entry_caption(puzzle_date)
    keyboard = daily_trivia_keyboard(include_subscribe=include_subscribe)
    send_kwargs = {
        "chat_id": user_id,
        "text": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": keyboard,
    }
    if reply_to_message_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_message_id
    try:
        await bot.send_message(**send_kwargs)
    except Exception as exc:
        logger.warning("daily trivia prompt failed user=%s: %s", user_id, exc)

    if mark_delivered:
        pref = await daily_delivery_repo.get_preference(user_id)
        await daily_delivery_repo.mark_daily_sent(
            user_id,
            puzzle_date,
            timezone=(pref or {}).get("timezone"),
        )


async def send_discovery_intro(bot, user_id: int) -> bool:
    text = (
        "🧠 <b>Новая daily-игра: Daily Trivia</b>\n\n"
        "Каждый день 5 уникальных вопросов обо всём на свете! "
        "Проверь свои знания и узнай интересные факты."
    )
    keyboard = daily_trivia_keyboard(include_subscribe=True)
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception as exc:
        logger.warning("daily trivia discovery failed user=%s: %s", user_id, exc)
    await daily_delivery_repo.mark_discovery_sent(user_id)
    return True


@safe_handler("Не удалось открыть Daily Trivia")
async def daily_trivia_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    today = today_puzzle_date(datetime.now(tz=UTC))
    await prepare_daily_puzzle(today)
    pref = await daily_delivery_repo.get_preference(user_id)
    is_subscribed = bool(pref and pref.get("is_subscribed"))

    await send_daily_trivia_entry(
        context.bot,
        user_id,
        today,
        include_subscribe=not is_subscribed,
        reply_to_message_id=update.effective_message.message_id,
        mark_delivered=False,
    )


async def check_daily_trivia_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    from app.handlers.daily_crocodile import is_daily_delivery_enabled

    now = datetime.now(tz=UTC)
    try:
        await ensure_prepared_puzzles(now=now)
    except Exception as exc:
        logger.error("daily trivia: ensure_prepared_puzzles failed: %s", exc, exc_info=True)
        return

    try:
        deleted = await trivia_repo.cleanup_old_used_keys(days=90)
        if deleted:
            logger.info("daily trivia: cleaned up %d expired used_keys entries", deleted)
    except Exception as exc:
        logger.warning("daily trivia: cleanup_old_used_keys failed: %s", exc)

    today = today_puzzle_date(now)
    if not await is_daily_delivery_enabled():
        logger.info("daily trivia delivery disabled by daily delivery switch")
        return

    due_delivery = await daily_delivery_repo.get_due_deliveries(puzzle_date=today, now=now)
    if due_delivery:
        sem = asyncio.Semaphore(10)

        async def _bounded_send(item):
            async with sem:
                try:
                    await send_daily_trivia_entry(context.bot, int(item["user_id"]), today)
                except Exception as exc:
                    logger.warning("daily trivia delivery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_send(item) for item in due_delivery])

    discovery = await daily_delivery_repo.get_discovery_candidates(now=now)
    if discovery:
        sem_disc = asyncio.Semaphore(10)

        async def _bounded_discovery(item):
            async with sem_disc:
                try:
                    await send_discovery_intro(context.bot, int(item["user_id"]))
                except Exception as exc:
                    logger.warning("daily trivia discovery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_discovery(item) for item in discovery])


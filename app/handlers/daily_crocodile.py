from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.config import settings
from app.repos import crocodile_daily as repo
from app.repos.daily_2048 import get_active_daily_game_mode, resolve_daily_game_mode
from app.repos.settings_repo import get_global_setting
from app.utils.decorators import safe_handler

logger = logging.getLogger(__name__)


async def _edit_callback_text(
    query,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Edit callback message, handling both text and photo (caption) messages.

    When the daily prompt was sent as a photo (placeholder image exists),
    the original message has no `.text` — only a caption.  Telegram's
    ``editMessageText`` rejects that with *"There is no text in the message
    to edit"*.  This helper detects the situation and falls back to
    ``edit_message_caption``.
    """
    msg = query.message
    if msg and msg.text is not None:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        # Photo / media message — edit the caption instead.
        await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)


_TIME_CHOICES = (9, 13, 19, 21)


async def _get_placeholder_file_id() -> str | InputFile | None:
    from app.games import cover_photo

    return await cover_photo.get_cover_photo("dailycroc")


def _webapp_base() -> str:
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
            [InlineKeyboardButton("Выбрать другую игру", callback_data="dailycroc:choose")],
            [InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe:crocodile")],
            [InlineKeyboardButton("Не напоминать 2 недели", callback_data="dailycroc:snooze")],
        ]
    )


def daily_play_keyboard(*, include_subscribe: bool = True) -> InlineKeyboardMarkup:
    rows = [[_play_button("Открыть daily")]]
    rows.append([InlineKeyboardButton("Выбрать другую игру", callback_data="dailycroc:choose")])
    if include_subscribe:
        rows.append([InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe:crocodile")])
    return InlineKeyboardMarkup(rows)


def daily_keyboard_for_game(game_mode: str, *, include_subscribe: bool = False) -> InlineKeyboardMarkup:
    if game_mode == "2048":
        from app.handlers.daily_2048 import daily2048_entry_keyboard

        return daily2048_entry_keyboard(include_subscribe=include_subscribe)
    if game_mode == "trivia":
        from app.handlers.daily_trivia import daily_trivia_keyboard

        return daily_trivia_keyboard(include_subscribe=include_subscribe)
    return daily_play_keyboard(include_subscribe=include_subscribe)


def subscribe_time_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{hour:02d}:00", callback_data=f"dailycroc:time:{hour}")] for hour in _TIME_CHOICES]
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


def _scheduled_daily_prompt_caption(puzzle_date: date) -> str:
    return (
        f"🐊 <b>Крокодил дня</b> · <code>{puzzle_date.isoformat()}</code>\n\n"
        "Сегодня уже готовы <b>Easy</b> и <b>Hard</b>. Оба режима можно пройти отдельно."
    )


def _manual_daily_prompt_caption() -> str:
    return (
        "🐊 <b>Крокодил дня</b>\n\n"
        "На сегодня доступны <b>Easy</b> и <b>Hard</b>. "
        "У каждого режима свои очки, completion и лидерборд."
    )


async def _send_daily_entry_message(
    bot,
    *,
    chat_id: int,
    user_id: int,
    puzzle_date: date,
    caption: str,
    include_subscribe: bool,
    reply_to_message_id: int | None = None,
    track_prompt_message: bool = True,
) -> bool:
    keyboard = daily_play_keyboard(include_subscribe=include_subscribe)
    placeholder_file_id = await _get_placeholder_file_id()
    send_kwargs = {
        "chat_id": chat_id,
        "parse_mode": ParseMode.HTML,
        "reply_markup": keyboard,
    }
    if reply_to_message_id is not None:
        send_kwargs["reply_to_message_id"] = reply_to_message_id
    if placeholder_file_id:
        try:
            msg = await bot.send_photo(
                photo=placeholder_file_id,
                caption=caption,
                **send_kwargs,
            )
            from app.games import cover_photo

            await cover_photo.remember_cover_file_id("dailycroc", msg)
            if track_prompt_message:
                await repo.register_prompt_message(
                    user_id=user_id,
                    puzzle_date=puzzle_date,
                    chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
            return True
        except Exception as exc:
            from app.games.daily_telegram_delivery import is_unreachable_chat_error

            if is_unreachable_chat_error(exc):
                raise
            logger.warning("daily prompt photo failed user=%s: %s — falling back to text", user_id, exc)
    await bot.send_message(
        text=caption,
        **send_kwargs,
    )
    return False


async def send_daily_prompt(
    bot,
    user_id: int,
    puzzle_date: date,
    *,
    include_subscribe: bool = False,
    reply_to_message_id: int | None = None,
    mark_delivered: bool = True,
    track_prompt_message: bool = True,
) -> bool:
    sent_as_photo = await _send_daily_entry_message(
        bot,
        chat_id=user_id,
        user_id=user_id,
        puzzle_date=puzzle_date,
        caption=_scheduled_daily_prompt_caption(puzzle_date),
        include_subscribe=include_subscribe,
        reply_to_message_id=reply_to_message_id,
        track_prompt_message=track_prompt_message,
    )
    pref = await repo.get_preference(user_id)
    if mark_delivered:
        await repo.mark_daily_sent(
            user_id,
            puzzle_date,
            timezone=(pref or {}).get("timezone"),
        )
    return sent_as_photo


# Daily Crocodile is intentionally public to any Telegram-authenticated user.
# Whitelist still gates the rest of the bot via command/message handlers.
@safe_handler("Не удалось открыть Крокодил дня")
async def dailycroc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    pref = await repo.get_preference(user_id)
    game_mode = resolve_daily_game_mode(pref, await get_active_daily_game_mode())
    await send_daily_menu_for_user(update, context, game_mode=game_mode, preference=pref)


async def send_daily_menu_for_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    game_mode: str,
    preference: dict | None = None,
) -> None:
    """Open a daily game or show the player's current result and leaderboard."""
    if not update.effective_user:
        return
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    user_id = update.effective_user.id
    reply_to_message_id = getattr(message, "message_id", None)
    pref = preference if preference is not None else await repo.get_preference(user_id)
    if game_mode == "2048":
        from app.games.daily_2048_telegram import render_result_body
        from app.handlers.daily_2048 import send_daily2048_entry
        from app.repos import daily_2048 as daily2048_repo

        today_2048 = daily2048_repo.today_puzzle_date(datetime.now(tz=UTC))
        await daily2048_repo.ensure_puzzle(today_2048)
        result = await daily2048_repo.get_result(user_id, today_2048)
        if result and result.status in {"won", "lost"}:
            text, keyboard = await render_result_body(user_id, today_2048)
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                reply_to_message_id=reply_to_message_id,
            )
            return
        await send_daily2048_entry(
            context.bot,
            user_id,
            today_2048,
            include_subscribe=not bool(pref and pref.get("is_subscribed")),
            reply_to_message_id=reply_to_message_id,
            mark_delivered=False,
        )
        return
    elif game_mode == "trivia":
        from app.handlers.daily_trivia import daily_trivia_command

        await daily_trivia_command(update, context)
        return
    await repo.record_player_activity(user_id, event="daily_played")
    is_subscribed = bool(pref and pref.get("is_subscribed"))
    now = datetime.now(tz=UTC)
    today = repo.today_puzzle_date(now)
    # Mark delivery so the scheduler doesn't send a duplicate today.
    if is_subscribed and not repo.was_daily_delivered_today(pref, puzzle_date=today, now=now):
        await repo.mark_daily_sent(
            user_id,
            today,
            now=now,
            timezone=(pref or {}).get("timezone"),
        )
    results = await repo.get_results_for_user(user_id, today)
    completed = next((mode for mode, result in results.items() if result.status in {"won", "lost"}), None)
    if completed:
        from app.games.crocodile_daily_telegram import render_daily_result_body

        text, croc_keyboard = await render_daily_result_body(user_id, today, focus_difficulty=completed)
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=croc_keyboard,
            reply_to_message_id=reply_to_message_id,
        )
        return
    await _send_daily_entry_message(
        context.bot,
        chat_id=update.effective_chat.id,
        user_id=user_id,
        puzzle_date=today,
        caption=_manual_daily_prompt_caption(),
        include_subscribe=not is_subscribed,
        reply_to_message_id=reply_to_message_id,
    )


async def daily_game_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if getattr(update.effective_chat, "type", "private") != "private":
        await query.answer("Выберите игру в личном чате с ботом", show_alert=True)
        return
    await query.answer()
    pref = await repo.get_preference(update.effective_user.id)
    selected = resolve_daily_game_mode(pref, await get_active_daily_game_mode())
    labels = {"crocodile": "🐊 Крокодил", "2048": "🎲 2048 Sprint", "trivia": "🧠 Викторина"}
    buttons = [
        [InlineKeyboardButton(f"{'✓ ' if mode == selected else ''}{label}", callback_data=f"dailycroc:game:{mode}")]
        for mode, label in labels.items()
    ]
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="Выберите игру дня. Выбор можно изменить в любое время; он действует и для ежедневной рассылки.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def daily_game_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or not query.data:
        return
    if getattr(update.effective_chat, "type", "private") != "private":
        await query.answer("Выберите игру в личном чате с ботом", show_alert=True)
        return
    game_mode = query.data.removeprefix("dailycroc:game:")
    if game_mode not in {"crocodile", "2048", "trivia"}:
        await query.answer("Неизвестная игра", show_alert=True)
        return
    await repo.upsert_preference(update.effective_user.id, daily_game=game_mode)
    await query.answer("Игра выбрана")
    await send_daily_menu_for_user(update, context, game_mode=game_mode)


async def daily_subscribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    game_mode = (query.data or "").removeprefix("dailycroc:subscribe:")
    if game_mode in {"crocodile", "2048", "trivia"}:
        await repo.upsert_preference(update.effective_user.id, daily_game=game_mode)
    else:
        await repo.upsert_preference(update.effective_user.id)
    await _edit_callback_text(
        query,
        "Когда присылать ежедневную игру?\n\nВыбери удобное местное время:",
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
    game_mode = resolve_daily_game_mode(pref, await get_active_daily_game_mode())
    game_label = {"crocodile": "Крокодила", "2048": "2048 Sprint", "trivia": "Викторину"}[game_mode]
    await _edit_callback_text(
        query,
        f"✅ Готово. Буду присылать {game_label} примерно в {hour:02d}:00.\n"
        f"Часовой пояс определю автоматически; сейчас используется {tz}.",
        reply_markup=daily_keyboard_for_game(game_mode),
    )


async def daily_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    until = await repo.snooze_discovery(update.effective_user.id)
    await query.answer("Не буду напоминать 2 недели")
    await _edit_callback_text(
        query,
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
    pref = await repo.get_preference(update.effective_user.id)
    game_mode = resolve_daily_game_mode(pref, await get_active_daily_game_mode())
    try:
        await query.edit_message_reply_markup(reply_markup=daily_keyboard_for_game(game_mode, include_subscribe=True))
    except Exception:
        pass


async def check_daily_crocodile_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    game_mode = await get_active_daily_game_mode()
    from app.handlers.daily_2048 import check_daily_2048_jobs
    from app.handlers.daily_trivia import check_daily_trivia_jobs

    # Every game is available to player-selected daily subscriptions. Each
    # handler sends only to its own recipients; the admin choice keeps its
    # longer preparation window and discovery invitation.
    outcomes = await asyncio.gather(
        check_daily_2048_jobs(context, admin_mode=game_mode),
        check_daily_trivia_jobs(context, admin_mode=game_mode),
        _check_daily_crocodile_only(context, game_mode),
        return_exceptions=True,
    )
    for game, outcome in zip(("2048", "trivia", "crocodile"), outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.error("daily %s scheduler failed: %s", game, type(outcome).__name__)


async def _check_daily_crocodile_only(context: ContextTypes.DEFAULT_TYPE, game_mode: str) -> None:
    from app.admin_alerts import AlertSeverity, alert_admin
    from app.games.crocodile_daily import active_daily_difficulties, ensure_prepared_puzzles

    now = datetime.now(tz=UTC)
    try:
        prepared = await ensure_prepared_puzzles(
            context.bot,
            now=now,
            days_ahead=repo.DAILY_PREP_DAYS_AHEAD if game_mode == "crocodile" else 1,
        )
    except Exception as exc:
        logger.error("daily Crocodile: ensure_prepared_puzzles failed: %s", exc, exc_info=True)
        await alert_admin(
            context.application,
            f"🐊 *Daily Croc* — `ensure_prepared_puzzles` завершился с ошибкой: `{type(exc).__name__}`",
            severity=AlertSeverity.CRITICAL,
            exc=exc,
        )
        return

    today = repo.today_puzzle_date(now)
    required = await active_daily_difficulties()
    today_puzzles = {item.difficulty: item for item in prepared if item.puzzle_date == today}
    if not today_puzzles:
        today_puzzles = await repo.get_puzzles_for_date(today)
    missing = [difficulty for difficulty in required if difficulty not in today_puzzles]
    if missing:
        logger.warning("daily Crocodile scheduler: puzzle missing for %s difficulties=%s", today, ",".join(missing))
        await alert_admin(
            context.application,
            f"🐊 *Daily Croc* — пазл отсутствует для `{today}` difficulties=`{','.join(missing)}`",
            severity=AlertSeverity.WARNING,
        )
        return
    not_ready = [difficulty for difficulty in required if not repo.is_puzzle_fully_prepared(today_puzzles[difficulty])]
    if not_ready:
        logger.warning(
            "daily Crocodile scheduler: puzzle %s not fully prepared for difficulties=%s; skipping sends",
            today,
            ",".join(not_ready),
        )
        return
    if not await is_daily_delivery_enabled():
        logger.info("daily Crocodile delivery disabled by admin switch; pre-generation kept running")
        return

    due_delivery = await repo.get_due_deliveries(
        puzzle_date=today, now=now, game_mode="crocodile", default_game_mode=game_mode
    )
    if due_delivery:
        # ⚡ Bolt Optimization: Send daily prompts concurrently with a safe concurrency limit (10)
        # to avoid blocking the job scheduler for O(N) seconds.
        sem = asyncio.Semaphore(10)

        async def _bounded_send(item):
            async with sem:
                try:
                    await send_daily_prompt(context.bot, int(item["user_id"]), today)
                except Exception as exc:
                    from app.games.daily_telegram_delivery import retire_unreachable_daily_recipient

                    if await retire_unreachable_daily_recipient(int(item["user_id"]), exc, discovery=False):
                        logger.info("daily Crocodile delivery stopped for unreachable chat")
                        return
                    logger.warning("daily Crocodile delivery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_send(item) for item in due_delivery])

    discovery = await repo.get_discovery_candidates(now=now) if game_mode == "crocodile" else []
    if discovery:
        sem_disc = asyncio.Semaphore(10)

        async def _bounded_discovery(item):
            async with sem_disc:
                try:
                    await send_discovery_intro(context.bot, int(item["user_id"]))
                except Exception as exc:
                    from app.games.daily_telegram_delivery import retire_unreachable_daily_recipient

                    if await retire_unreachable_daily_recipient(int(item["user_id"]), exc, discovery=True):
                        logger.info("daily Crocodile discovery snoozed for unreachable chat")
                        return
                    logger.warning("daily Crocodile discovery failed user=%s: %s", item.get("user_id"), exc)

        await asyncio.gather(*[_bounded_discovery(item) for item in discovery])

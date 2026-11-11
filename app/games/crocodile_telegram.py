from __future__ import annotations

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from app.utils.background_tasks import try_submit_task

logger = logging.getLogger(__name__)

_THERMOMETER_DEBOUNCE_S = 2.0
_pending_thermometer_updates: dict[str, dict] = {}
_thermometer_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]


async def _retire_thermometer(inline_message_id: str) -> None:
    _pending_thermometer_updates.pop(inline_message_id, None)
    owner = _thermometer_tasks.get(inline_message_id)
    if owner is None or owner is asyncio.current_task():
        return
    if not owner.done() and not owner.cancelling():
        owner.cancel()
    joined = asyncio.gather(owner, return_exceptions=True)
    cancellation = None
    while not joined.done():
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    joined.result()
    if cancellation is not None:
        raise cancellation


def _score_bar(score: float, width: int = 10) -> str:
    filled = round(score * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _score_emoji(score: float) -> str:
    if score >= 0.92:
        return "🎉"
    if score >= 0.7:
        return "🔥"
    if score >= 0.3:
        return "🟡"
    return "🧊"


def _build_play_keyboard(bot, game_id: str) -> InlineKeyboardMarkup | None:
    from app.config import settings

    miniapp_short = (getattr(settings, "MINIAPP_SHORT_NAME", "") or "").strip()
    bot_username = (getattr(bot, "username", "") or "").strip()
    if miniapp_short and bot_username:
        url = f"https://t.me/{bot_username}/{miniapp_short}?startapp={game_id}"
    else:
        webapp_base = (getattr(settings, "WEBAPP_BASE_URL", "") or "").strip().rstrip("/")
        if not webapp_base:
            webhook_url = os.environ.get("WEBHOOK_URL", "") or ""
            webapp_base = webhook_url.split("/webhook")[0].rstrip("/")
        if not webapp_base:
            return None
        url = f"{webapp_base}/webapp/game?game_id={game_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Играть", url=url)]])


class CrocodileTelegramService:
    @staticmethod
    async def finalize_game(bot, game) -> None:
        await _retire_thermometer(game.inline_message_id)
        if game.status == "won":
            text = (
                f"🎉 <b>Угадано!</b> Слово: <b>{game.target_word.upper()}</b>\n"
                f"<i>Попыток: {len(game.attempts)} из {game.max_attempts}</i>"
            )
        elif game.status == "lost" and not game.attempts:
            text = f"🏳️ <b>Игрок сдался.</b> Слово было: <b>{game.target_word.upper()}</b>"
        else:
            text = (
                f"😔 <b>Не угадали.</b> Слово было: <b>{game.target_word.upper()}</b>\n"
                f"<i>Израсходовано {len(game.attempts)} попыток</i>"
            )

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Получать каждый день", callback_data="dailycroc:subscribe")]]
        )

        await bot.edit_message_text(
            inline_message_id=game.inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    @staticmethod
    async def send_thermometer_update(bot, game) -> None:
        emoji = _score_emoji(game.best_score)
        bar = _score_bar(game.best_score)
        pct = int(game.best_score * 100)
        text = f"🎯 <b>Крокодил</b> — идёт игра\n{emoji} Лучшая попытка: <code>{bar}</code> {pct}%"
        keyboard = _build_play_keyboard(bot, game.game_id)
        await bot.edit_message_text(
            inline_message_id=game.inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    @classmethod
    def queue_thermometer_update(cls, bot, game) -> None:
        inline_message_id = game.inline_message_id
        task = _thermometer_tasks.get(inline_message_id)
        if task and not task.done() and task.cancelling():
            return
        _pending_thermometer_updates[inline_message_id] = {
            "bot": bot,
            "game_id": game.game_id,
            "inline_message_id": inline_message_id,
            "best_score": game.best_score,
        }
        if task and not task.done():
            return
        cls._start_thermometer_flush(inline_message_id)

    @classmethod
    def _start_thermometer_flush(cls, inline_message_id: str) -> None:
        _thermometer_tasks.pop(inline_message_id, None)
        task = try_submit_task(cls._flush_thermometer_update(inline_message_id), operation="game.thermometer_refresh")
        if task is None:
            _pending_thermometer_updates.pop(inline_message_id, None)
            return
        _thermometer_tasks[inline_message_id] = task

        def completed(owner: asyncio.Task) -> None:
            if _thermometer_tasks.get(inline_message_id) is not owner:
                return
            _thermometer_tasks.pop(inline_message_id, None)
            _pending_thermometer_updates.pop(inline_message_id, None)

        task.add_done_callback(completed)

    @classmethod
    async def _flush_thermometer_update(cls, inline_message_id: str) -> None:
        owner = asyncio.current_task()
        try:
            while True:
                try:
                    await asyncio.sleep(_THERMOMETER_DEBOUNCE_S)
                    snapshot = _pending_thermometer_updates.pop(inline_message_id, None)
                    if not snapshot:
                        return
                    bot = snapshot["bot"]
                    game_id = str(snapshot.get("game_id") or "")
                    from app.games.crocodile import load_game
                    from app.games.crocodile_runtime import game_mutation_lock

                    # Serialize the refresh with accepted guesses/finalization.
                    # A fresh owned read also excludes late snapshots queued by
                    # another replica after the terminal game was removed.
                    async with game_mutation_lock(game_id):
                        current = await load_game(game_id)
                        if (
                            current is not None
                            and current.status == "active"
                            and current.inline_message_id == inline_message_id
                        ):
                            await cls.send_thermometer_update(bot, current)
                except Exception as exc:
                    logger.debug("queue_thermometer_update failed inline=%s: %s", inline_message_id, exc)
                if inline_message_id not in _pending_thermometer_updates:
                    return
        finally:
            if _thermometer_tasks.get(inline_message_id) is owner:
                _thermometer_tasks.pop(inline_message_id, None)
                _pending_thermometer_updates.pop(inline_message_id, None)


def reset_telegram_state_for_tests() -> None:
    for task in list(_thermometer_tasks.values()):
        if not task.done():
            task.cancel()
    _thermometer_tasks.clear()
    _pending_thermometer_updates.clear()

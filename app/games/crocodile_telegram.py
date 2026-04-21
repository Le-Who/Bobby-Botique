from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

_THERMOMETER_DEBOUNCE_S = 2.0
_pending_thermometer_updates: dict[str, dict] = {}
_thermometer_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]


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


class CrocodileTelegramService:
    @staticmethod
    async def finalize_game(bot, game) -> None:
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

        await bot.edit_message_text(
            inline_message_id=game.inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([]),
        )

    @staticmethod
    async def send_thermometer_update(bot, game) -> None:
        emoji = _score_emoji(game.best_score)
        bar = _score_bar(game.best_score)
        pct = int(game.best_score * 100)
        text = f"🎯 <b>Крокодил</b> — идёт игра\n{emoji} Лучшая попытка: <code>{bar}</code> {pct}%"
        await bot.edit_message_text(
            inline_message_id=game.inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )

    @classmethod
    def queue_thermometer_update(cls, bot, game) -> None:
        inline_message_id = game.inline_message_id
        _pending_thermometer_updates[inline_message_id] = {
            "bot": bot,
            "game_id": game.game_id,
            "inline_message_id": inline_message_id,
            "best_score": game.best_score,
        }
        task = _thermometer_tasks.get(inline_message_id)
        if task and not task.done():
            return
        _thermometer_tasks[inline_message_id] = asyncio.create_task(cls._flush_thermometer_update(inline_message_id))

    @classmethod
    async def _flush_thermometer_update(cls, inline_message_id: str) -> None:
        try:
            await asyncio.sleep(_THERMOMETER_DEBOUNCE_S)
            snapshot = _pending_thermometer_updates.pop(inline_message_id, None)
            if not snapshot:
                return
            bot = snapshot["bot"]
            best_score = float(snapshot["best_score"])
            emoji = _score_emoji(best_score)
            bar = _score_bar(best_score)
            pct = int(best_score * 100)
            text = f"🎯 <b>Крокодил</b> — идёт игра\n{emoji} Лучшая попытка: <code>{bar}</code> {pct}%"
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("queue_thermometer_update failed inline=%s: %s", inline_message_id, exc)
        finally:
            _thermometer_tasks.pop(inline_message_id, None)
            if inline_message_id in _pending_thermometer_updates:
                _thermometer_tasks[inline_message_id] = asyncio.create_task(cls._flush_thermometer_update(inline_message_id))


def reset_telegram_state_for_tests() -> None:
    for task in list(_thermometer_tasks.values()):
        if not task.done():
            task.cancel()
    _thermometer_tasks.clear()
    _pending_thermometer_updates.clear()

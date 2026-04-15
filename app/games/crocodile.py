# /app/games/crocodile.py
"""CrocodileGame — state machine for a single game session.

Lifecycle:
  CREATE  → game stored in Redis with TTL 10 min
  ACTIVE  → guesses arrive via WebSocket
  WON     → exact_match registered; inline message updated; Redis cleaned
  LOST    → max_attempts reached; inline message updated; Redis cleaned

Redis keys:
  croc:game:<game_id>   — JSON blob with game state, TTL 10 min (600s)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

_GAME_KEY_PREFIX = "croc:game:"
_GAME_TTL = 600  # 10 minutes
_MAX_ATTEMPTS = 10


# ── Game dataclass ────────────────────────────────────────────────────────────


@dataclass
class CrocodileGame:
    """Full state for one game session."""

    game_id: str
    target_word: str
    category: str
    lang: str
    inline_message_id: str
    creator_id: int
    guesser_id: int | None  # Set on first WS connect
    attempts: list[str] = field(default_factory=list)
    max_attempts: int = _MAX_ATTEMPTS
    status: Literal["active", "won", "lost"] = "active"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # ── Redis persistence ─────────────────────────────────────────────────────

    def _redis_key(self) -> str:
        return f"{_GAME_KEY_PREFIX}{self.game_id}"

    def to_json(self) -> str:
        return json.dumps(
            {
                "game_id": self.game_id,
                "target_word": self.target_word,
                "category": self.category,
                "lang": self.lang,
                "inline_message_id": self.inline_message_id,
                "creator_id": self.creator_id,
                "guesser_id": self.guesser_id,
                "attempts": self.attempts,
                "max_attempts": self.max_attempts,
                "status": self.status,
                "created_at": self.created_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> CrocodileGame:
        d = json.loads(data)
        # Whitelist known fields to guard against Redis data corruption.
        known = {
            "game_id", "target_word", "category", "lang", "inline_message_id",
            "creator_id", "guesser_id", "attempts", "max_attempts",
            "status", "created_at",
        }
        return cls(**{k: v for k, v in d.items() if k in known})

    async def save(self) -> bool:
        """Persist game to Redis; fall back to in-memory store on failure."""
        try:
            from app.cache import redis_client

            if redis_client:
                await redis_client.set(self._redis_key(), self.to_json().encode(), ex=_GAME_TTL)  # type: ignore[misc]
                return True
        except Exception as exc:
            logger.warning("CrocodileGame.save failed game=%s: %s", self.game_id, exc)

        # Redis unavailable or failed — fall back to bounded in-memory dict.
        _mem_put(self)
        return False

    async def delete(self) -> None:
        """Remove game from Redis on termination."""
        try:
            from app.cache import redis_client

            if redis_client:
                await redis_client.delete(self._redis_key())  # type: ignore[misc]
        except Exception as exc:
            logger.debug("CrocodileGame.delete failed game=%s: %s", self.game_id, exc)

    # ── Guess handling ────────────────────────────────────────────────────────

    async def process_guess(self, word: str) -> dict:
        """Evaluate the guess and mutate state.

        Returns:
            A dict ready to serialize as a WebSocket event:
            {event, status, score, hint, attempts, max_attempts, [word]}
        """
        from app.games.judge import judge_guess

        word = word.strip()
        if not word:
            return {"event": "error", "message": "Empty guess"}

        self.attempts.append(word)
        status_str, judgement = await judge_guess(self.target_word, word)

        event: dict = {
            "event": "result",
            "status": status_str,
            "score": judgement.score,
            "hint": judgement.hint,
            "attempts": len(self.attempts),
            "max_attempts": self.max_attempts,
            "cached": judgement.cached,
        }

        if status_str == "exact_match":
            self.status = "won"
            event["word"] = self.target_word
        elif len(self.attempts) >= self.max_attempts:
            self.status = "lost"
            event["event"] = "game_over"
            event["reason"] = "max_attempts"
            event["word"] = self.target_word

        await self.save()
        return event

    # ── Finalise (update Telegram message) ───────────────────────────────────

    async def finalize(self, bot) -> None:
        """Edit the inline message in Telegram to reflect the game outcome."""
        from telegram import InlineKeyboardMarkup

        if self.status == "won":
            text = (
                f"🎉 <b>Угадано!</b> Слово: <b>{self.target_word.upper()}</b>\n"
                f"<i>Попыток: {len(self.attempts)} из {self.max_attempts}</i>"
            )
        else:
            text = (
                f"😔 <b>Не угадали.</b> Слово было: <b>{self.target_word.upper()}</b>\n"
                f"<i>Израсходовано {self.max_attempts} попыток</i>"
            )

        try:
            await bot.edit_message_text(
                inline_message_id=self.inline_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception as exc:
            logger.warning(
                "CrocodileGame.finalize: edit_message_text failed game=%s: %s",
                self.game_id,
                exc,
            )

        await self.delete()


# ── Factory helpers ───────────────────────────────────────────────────────────


async def create_game(
    *,
    target_word: str,
    category: str,
    lang: str,
    inline_message_id: str,
    creator_id: int,
) -> CrocodileGame:
    """Create and persist a new game."""
    game = CrocodileGame(
        game_id=str(uuid.uuid4()),
        target_word=target_word,
        category=category,
        lang=lang,
        inline_message_id=inline_message_id,
        creator_id=creator_id,
        guesser_id=None,
    )
    await game.save()
    logger.info(
        "Created game %s word=%r category=%s/%s",
        game.game_id,
        target_word,
        lang,
        category,
    )
    return game


# ── In-memory fallback (Redis-less environments) ──────────────────────────────
# Bounded to 64 active games; each ≤1KB. Evicted LRU on insertion overflow.

_mem_games: dict[str, CrocodileGame] = {}
_MEM_MAX = 64


def _mem_put(game: CrocodileGame) -> None:
    if len(_mem_games) >= _MEM_MAX:
        # Evict oldest inserted entry
        oldest = next(iter(_mem_games))
        _mem_games.pop(oldest, None)
    _mem_games[game.game_id] = game


def _mem_get(game_id: str) -> CrocodileGame | None:
    return _mem_games.get(game_id)


async def load_game(game_id: str) -> CrocodileGame | None:
    """Load a game — Redis primary, in-memory fallback.

    The fallback is transparent: callers never need to care whether Redis is
    available. This function is the single canonical entry point; there is no
    module-level monkey-patching.
    """
    try:
        from app.cache import redis_client

        if redis_client:
            raw = await redis_client.get(f"{_GAME_KEY_PREFIX}{game_id}")  # type: ignore[misc]
            if raw is not None:
                return CrocodileGame.from_json(raw)
    except Exception as exc:
        logger.warning("load_game Redis failed game=%s: %s", game_id, exc)

    # Redis miss or unavailable — try in-memory store.
    return _mem_get(game_id)

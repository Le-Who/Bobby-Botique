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

import asyncio
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

    # ── Guess handling ────────────────────────────────────────────────────────────

    async def process_guess(self, word: str) -> dict:
        """Evaluate the guess and mutate state.

        Returns:
            A dict ready to serialise as a WebSocket event.
            Possible events: 'result', 'game_over', 'judge_unavailable', 'error'.

        If status_str is 'judge_unavailable', the attempt is NOT counted and
        the caller must not decrement any client-side counter.
        """
        from app.games.judge import judge_guess

        word = word.strip()
        if not word:
            return {"event": "error", "message": "Empty guess"}

        status_str, judgement = await judge_guess(self.target_word, word)

        # LLM race failed — do NOT record the attempt; let the player retry.
        if status_str == "judge_unavailable":
            return {
                "event": "judge_unavailable",
                "message": "🤔 Крокодил слишком глубоко задумался... Попытка не засчитана, попробуй ещё раз!",
            }

        self.attempts.append(word)

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

        # Record in per-game in-memory history (not in Redis)
        _mem_history.setdefault(self.game_id, []).append({
            "word": word,
            "status": status_str,
            "hint": judgement.hint,
            "score": judgement.score,
        })

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
    """Create and persist a new game, then start background hint generation."""
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
    # Pre-generate 3 progressive hints in background so they are ready when
    # the guesser connects. Non-blocking — failure is silently swallowed.
    asyncio.create_task(  # noqa: RUF006
        _prefetch_hints(game.game_id, target_word, category)
    )
    return game


async def _prefetch_hints(game_id: str, word: str, category: str) -> None:
    """Generate 3 progressive hints and store them in _mem_hints[game_id].

    Checks hints_cache first so repeat sessions with the same word pay 0 LLM
    tokens. Results are keyed by game_id so they vanish when the game ends.
    """
    from app.games.judge import generate_hints
    from app.games.judgement_cache import cache_hints, get_cached_hints

    cached = await get_cached_hints(word, category)
    if cached:
        _mem_hints[game_id] = cached
        logger.debug("Hints cache hit for word=%r game=%s", word, game_id)
        return

    hints = await generate_hints(word, category)
    if hints:
        _mem_hints[game_id] = hints
        await cache_hints(word, category, hints)  # persist for future games
        logger.debug("Hints generated for word=%r game=%s", word, game_id)
    else:
        logger.warning("Hint generation failed for word=%r game=%s", word, game_id)


# ── In-memory side stores (never serialised to Redis) ────────────────────────
# These hold ephemeral per-game data that is:
#   • Too large / too transient for Redis (hints are ~200B per game)
#   • Regenerated automatically if the process restarts

# game_id → list of 3 progressive hint strings (generated by LLM at game start)
_mem_hints: dict[str, list[str]] = {}

# game_id → ordered list of guess result dicts (for chat history restore)
_mem_history: dict[str, list[dict]] = {}

# ── Public accessors ────────────────────────────────────────────────────────────


def get_game_hints(game_id: str) -> list[str]:
    """Return the pre-generated hint list for this game, or []."""
    return _mem_hints.get(game_id, [])


def get_game_history(game_id: str) -> list[dict]:
    """Return the full guess history for this game (for WS reconnect restore)."""
    return _mem_history.get(game_id, [])


# ── In-memory game fallback (Redis-less environments) ───────────────────────
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

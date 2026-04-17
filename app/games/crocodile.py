# /app/games/crocodile.py
"""CrocodileGame — state machine for a single game session.

Lifecycle:
  CREATE  → game stored in Redis with no TTL (idle — waiting for guesser)
  ACTIVE  → guesses arrive via WebSocket; each guess resets TTL to 20 min
  WON     → exact_match registered; inline message updated; Redis cleaned
  LOST    → max_attempts reached; inline message updated; Redis cleaned

Redis keys:
  croc:game:<game_id>   — JSON blob with game state
    Idle TTL  = 14 days (expiration before first guesser joins)
    Active TTL = 2 days (sliding window — resets on every guess attempt)
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
# Two-phase TTL for better UX:
#   IDLE   — game created, waiting for a guesser to open the link (14 days)
#   ACTIVE — guesser is playing; reset on every guess so they can think (2 days)
_GAME_TTL_IDLE = 14 * 24 * 60 * 60  # 14 days
_GAME_TTL_ACTIVE = 2 * 24 * 60 * 60  # 2 days
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
    has_activity: bool = False  # True after the first real guess attempt, even if not counted
    max_attempts: int = _MAX_ATTEMPTS
    status: Literal["active", "won", "lost"] = "active"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Best semantic score seen so far across all guesses (0.0–1.0).
    # Persisted to Redis so the inline thermometer survives WS reconnects.
    best_score: float = 0.0

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
                "has_activity": self.has_activity,
                "max_attempts": self.max_attempts,
                "status": self.status,
                "created_at": self.created_at,
                "best_score": self.best_score,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str | bytes) -> CrocodileGame:
        d = json.loads(data)
        # Whitelist known fields to guard against Redis data corruption.
        known = {
            "game_id",
            "target_word",
            "category",
            "lang",
            "inline_message_id",
            "creator_id",
            "guesser_id",
            "attempts",
            "has_activity",
            "max_attempts",
            "status",
            "created_at",
            "best_score",
        }
        return cls(**{k: v for k, v in d.items() if k in known})

    async def save(self) -> bool:
        """Persist game to Redis; fall back to in-memory store on failure.

        TTL is two-phase:
          - No guesser yet (idle)  → 14 days. Gives unlimited practical
            time for the creator to share the link and the guesser to open it.
          - Guesser joined & playing → _GAME_TTL_ACTIVE (2 days) sliding window.
            Resets on every guess attempt so the player can step away and think.
        """
        ttl_kwargs = {"ex": _GAME_TTL_ACTIVE if self.guesser_id is not None else _GAME_TTL_IDLE}
        try:
            from app.cache import redis_client

            if redis_client:
                await redis_client.set(self._redis_key(), self.to_json().encode(), **ttl_kwargs)  # type: ignore[misc]
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
        from app.games.judge import judge_guess, score_bar, score_emoji

        word = word.strip()
        if not word:
            return {"event": "error", "message": "Empty guess"}

        status_str, judgement = await judge_guess(self.target_word, word)
        self.has_activity = True

        # LLM race failed — do NOT record the attempt; let the player retry.
        if status_str == "judge_unavailable":
            await self.save()
            return {
                "event": "judge_unavailable",
                "message": "🤔 Крокодил слишком глубоко задумался... Попытка не засчитана, попробуй ещё раз!",
            }

        self.attempts.append(word)

        # Track the all-time best score for the inline thermometer.
        # exact_match always forces score=1.0 by the judge.
        best_score_updated = judgement.score > self.best_score
        if best_score_updated:
            self.best_score = judgement.score

        # Emoji prefix for instant color-coded temperature feedback.
        prefixed_hint = f"{score_emoji(judgement.score)} {judgement.hint}"

        event: dict = {
            "event": "result",
            "status": status_str,
            "score": judgement.score,
            "score_bar": score_bar(judgement.score),
            "hint": prefixed_hint,
            "attempts": len(self.attempts),
            "max_attempts": self.max_attempts,
            "cached": judgement.cached,
            "best_score": self.best_score,
            "best_score_updated": best_score_updated,
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
        _mem_history.setdefault(self.game_id, []).append(
            {
                "word": word,
                "status": status_str,
                "hint": prefixed_hint,
                "score": judgement.score,
            }
        )

        await self.save()
        return event

    # ── Finalise (update Telegram message) ───────────────────────────────────────

    async def finalize(self, bot) -> None:
        """Edit the inline message in Telegram to reflect the game outcome."""
        from telegram import InlineKeyboardMarkup

        if self.status == "won":
            text = (
                f"🎉 <b>Угадано!</b> Слово: <b>{self.target_word.upper()}</b>\n"
                f"<i>Попыток: {len(self.attempts)} из {self.max_attempts}</i>"
            )
        elif self.status == "lost" and not self.attempts:
            # Surrender with zero guesses — distinct phrasing
            text = f"🏳️ <b>Игрок сдался.</b> Слово было: <b>{self.target_word.upper()}</b>"
        else:
            text = (
                f"😔 <b>Не угадали.</b> Слово было: <b>{self.target_word.upper()}</b>\n"
                f"<i>Израсходовано {len(self.attempts)} попыток</i>"
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

    async def update_inline_thermometer(self, bot) -> None:
        """Edit the inline PM message to display the current best-guess temperature.

        Called fire-and-forget after every new best_score so the creator and
        guesser can see progress right in the private chat without opening the
        WebApp. All Telegram errors are swallowed silently.
        """
        from app.games.judge import score_bar, score_emoji

        emoji = score_emoji(self.best_score)
        bar = score_bar(self.best_score)
        pct = int(self.best_score * 100)
        text = f"🎯 <b>Крокодил</b> — идёт игра\n{emoji} Лучшая попытка: <code>{bar}</code> {pct}%"
        try:
            from telegram.constants import ParseMode

            await bot.edit_message_text(
                inline_message_id=self.inline_message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            # 400 "message is not modified" is normal when score did not change
            logger.debug("update_inline_thermometer failed game=%s: %s", self.game_id, exc)

    async def surrender(self, bot) -> dict:
        """Игрок сдаётся — раскрывает слово и завершает игру.

        Sets status to 'lost', calls finalize() to update the inline message,
        and returns a WebSocket event payload the handler can forward to both
        participants.
        """
        self.status = "lost"
        await self.finalize(bot)
        return {
            "event": "surrendered",
            "word": self.target_word,
            "attempts": len(self.attempts),
        }


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

# game_id → list of asyncio.Queue subscribers (Spectator / God Mode PubSub)
# Each WebSocket handler that needs live broadcasts registers a Queue here.
_game_subscribers: dict[str, list[asyncio.Queue]] = {}  # type: ignore[type-arg]

# ── Public accessors ────────────────────────────────────────────────────────────


def get_game_hints(game_id: str) -> list[str]:
    """Return the pre-generated hint list for this game, or []."""
    return _mem_hints.get(game_id, [])


def get_game_history(game_id: str) -> list[dict]:
    """Return the full guess history for this game (for WS reconnect restore)."""
    return _mem_history.get(game_id, [])


# ── Pub/Sub helpers ──────────────────────────────────────────────────────────


def subscribe_game(game_id: str) -> asyncio.Queue:  # type: ignore[type-arg]
    """Register a new subscriber queue for the given game and return it.

    Each WebSocket handler (guesser or creator/spectator) that wants live
    broadcasts calls this once at connect time, and passes the returned Queue
    to its receive loop.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=64)  # type: ignore[type-arg]
    _game_subscribers.setdefault(game_id, []).append(q)
    return q


def unsubscribe_game(game_id: str, q: asyncio.Queue) -> None:  # type: ignore[type-arg]
    """Remove a subscriber queue when the WebSocket closes."""
    subs = _game_subscribers.get(game_id)
    if subs:
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _game_subscribers.pop(game_id, None)


async def broadcast_game_event(game_id: str, payload: dict, exclude: asyncio.Queue | None = None) -> None:  # type: ignore[type-arg]
    """Fan-out a JSON-serialisable payload to all subscribers except *exclude*.

    Drops the event for any subscriber whose queue is full (back-pressure),
    so one slow client cannot block the entire broadcast.
    """
    subs = _game_subscribers.get(game_id, [])
    for q in list(subs):  # iterate a snapshot in case unsubscribe runs concurrently
        if q is exclude:
            continue
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("broadcast_game_event: queue full for game=%s, dropping event", game_id)


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

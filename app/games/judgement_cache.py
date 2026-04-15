# /app/games/judgement_cache.py
"""Redis-backed judgement cache for Crocodile game guess evaluation.

Caches LLM judgement results for (target_word, guess_word) pairs so that
common combinations (e.g. крокодил↔аллигатор) are served in <5ms on
subsequent games without spending LLM tokens.

Cache key format: ``croc:judge:<md5(target:guess)>``
TTL: 24 hours (86 400s)
Max estimated volume: ~10 000 entries × ~150B = ~1.5MB — negligible.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.games.judge import GuessJudgement

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "croc:judge:"
_CACHE_TTL = 86_400  # 24 hours


def _cache_key(target: str, guess: str) -> str:
    """Build a normalised, deterministic Redis key for the pair."""
    pair = f"{target.lower().strip()}:{guess.lower().strip()}"
    return f"{_CACHE_PREFIX}{hashlib.md5(pair.encode()).hexdigest()}"


async def get_cached_judgement(target: str, guess: str) -> GuessJudgement | None:
    """Look up a cached judgement. Returns None on miss or Redis error."""
    try:
        from app.cache import redis_client
        from app.games.judge import GuessJudgement

        if not redis_client:
            return None

        raw = await redis_client.get(_cache_key(target, guess))  # type: ignore[misc]
        if raw is None:
            return None

        data = raw.decode() if isinstance(raw, bytes) else raw
        return GuessJudgement.model_validate_json(data)

    except Exception as exc:
        logger.debug("Judgement cache GET failed (%s↔%s): %s", target, guess, exc)
        return None


async def cache_judgement(target: str, guess: str, result: GuessJudgement) -> None:
    """Store a judgement in Redis with TTL. Silently swallows errors."""
    try:
        from app.cache import redis_client

        if not redis_client:
            return

        await redis_client.set(  # type: ignore[misc]
            _cache_key(target, guess),
            result.model_dump_json(),
            ex=_CACHE_TTL,
        )
    except Exception as exc:
        logger.debug("Judgement cache SET failed (%s↔%s): %s", target, guess, exc)

# /app/games/judgement_cache.py
"""Local-file judgement cache for Crocodile game guess evaluation.

Caches LLM judgement results for (target_word, guess_word) pairs so that
common combinations (e.g. крокодил↔аллигатор) are served in <1ms on
subsequent games without spending LLM tokens or Redis keys.

Storage:
  - In-process: OrderedDict (LRU, max _MAX_ENTRIES entries) — sub-ms reads.
  - Persistence: JSON file at _CACHE_PATH, written-through on every new entry
    and loaded once at module import. File is ~a few hundred KB at full cap.

Max capacity: _MAX_ENTRIES = 50 000 entries x ~150B ~ 7 MB on disk.
No TTL: cache is evicted by LRU capacity, not time. Old entries for obscure
word pairs are naturally evicted when the cap is hit.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.games.judge import GuessJudgement

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

# Directory for persistent game data alongside the module itself
_DATA_DIR = Path(__file__).parent / "data"
_CACHE_PATH = _DATA_DIR / "judgement_cache.json"
_MAX_ENTRIES = 50_000


# ── In-process LRU store ──────────────────────────────────────────────────────

def _make_key(target: str, guess: str) -> str:
    """Normalised, deterministic cache key for the pair."""
    return f"{target.lower().strip()}:{guess.lower().strip()}"


# OrderedDict used as LRU: most recently accessed at end, oldest at front.
_store: OrderedDict[str, str] = OrderedDict()


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_from_disk() -> None:
    """Load the JSON cache file into _store. Called once at import time."""
    if not _CACHE_PATH.exists():
        return
    try:
        raw: dict[str, str] = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        _store.update(raw)
        # Enforce cap in case file grew beyond limit before a code update
        while len(_store) > _MAX_ENTRIES:
            _store.popitem(last=False)
        logger.debug("Judgement cache loaded: %d entries from %s", len(_store), _CACHE_PATH)
    except Exception as exc:
        logger.warning("Judgement cache load failed (%s): %s — starting empty", _CACHE_PATH, exc)


def _persist() -> None:
    """Write the current _store to disk. Silently swallows errors."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(_store), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as exc:
        logger.debug("Judgement cache persist failed: %s", exc)


# Load on module import (synchronous — JSON parse is fast, <10ms even at 50k entries)
_load_from_disk()


# ── Public API ────────────────────────────────────────────────────────────────


async def get_cached_judgement(target: str, guess: str) -> GuessJudgement | None:
    """Look up a cached judgement. Returns None on miss."""
    from app.games.judge import GuessJudgement

    key = _make_key(target, guess)
    value = _store.get(key)
    if value is None:
        return None

    # Move to end (LRU: mark as recently used)
    _store.move_to_end(key)

    try:
        return GuessJudgement.model_validate_json(value)
    except Exception as exc:
        logger.debug("Judgement cache deserialise failed (%s↔%s): %s", target, guess, exc)
        _store.pop(key, None)
        return None


async def cache_judgement(target: str, guess: str, result: GuessJudgement) -> None:
    """Store a judgement in the in-process LRU cache and write through to disk."""
    key = _make_key(target, guess)

    # Evict oldest entry if at capacity
    if key not in _store and len(_store) >= _MAX_ENTRIES:
        _store.popitem(last=False)

    _store[key] = result.model_dump_json()
    _store.move_to_end(key)

    _persist()

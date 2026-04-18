# /app/games/judgement_cache.py
"""Local-file judgement cache for Crocodile game guess evaluation.

Caches LLM judgement results for (target_word, guess_word) pairs so that
common combinations (e.g. крокодил↔аллигатор) are served in <1ms on
subsequent games without spending LLM tokens or Redis keys.

Storage:
  - In-process: OrderedDict (LRU, max _MAX_ENTRIES entries) — sub-ms reads.
  - Persistence: JSON file at _CACHE_PATH, written-through on every new entry
    and loaded once at module import. File is ~a few hundred KB at full cap.
  - Persist writes are dispatched to asyncio.to_thread() so they never block
    the event loop.

Max capacity: _MAX_ENTRIES = 50 000 entries x ~150B ~ 7 MB on disk.
No TTL: cache is evicted by LRU capacity, not time. Old entries for obscure
word pairs are naturally evicted when the cap is hit.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from app.utils.json_compat import json

if TYPE_CHECKING:
    from app.games.judge import GuessJudgement

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

# Directory for persistent game data alongside the module itself
_DATA_DIR = Path(__file__).parent / "data"
_CACHE_PATH = _DATA_DIR / "judgement_cache.json"
_HINTS_CACHE_PATH = _DATA_DIR / "hints_cache.json"
_CAT_CACHE_PATH = _DATA_DIR / "category_cache.json"
_MAX_ENTRIES = 50_000
_MAX_HINTS = 5_000
_MAX_CAT = 10_000


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


def _persist_sync() -> None:
    """Write the current _store to disk. Runs inside to_thread — never call from async directly."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(_store), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except Exception as exc:
        logger.debug("Judgement cache persist failed: %s", exc)


async def _persist() -> None:
    """Async wrapper: off-load blocking file I/O to a thread so events loop stays free."""
    await asyncio.to_thread(_persist_sync)


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

    await _persist()


# ── Hints cache ──────────────────────────────────────────────────────────────
# Caches LLM-generated progressive hints (list[str]) keyed by word+category.
# Stored separately from judgements — different shape, different eviction rate.

_hints_store: OrderedDict[str, str] = OrderedDict()


def _hints_key(word: str, category: str) -> str:
    return f"{word.lower().strip()}\x00{category.lower().strip()}"


def _load_hints_from_disk() -> None:
    """Load the hints JSON file into _hints_store. Called once at import."""
    if not _HINTS_CACHE_PATH.exists():
        return
    try:
        raw: dict[str, str] = json.loads(_HINTS_CACHE_PATH.read_text(encoding="utf-8"))
        _hints_store.update(raw)
        while len(_hints_store) > _MAX_HINTS:
            _hints_store.popitem(last=False)
        logger.debug("Hints cache loaded: %d entries", len(_hints_store))
    except Exception as exc:
        logger.warning("Hints cache load failed: %s — starting empty", exc)


def _persist_hints_sync() -> None:
    """Write _hints_store to disk. Runs inside to_thread."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _HINTS_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(_hints_store), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_HINTS_CACHE_PATH)
    except Exception as exc:
        logger.debug("Hints cache persist failed: %s", exc)


async def _persist_hints() -> None:
    """Async wrapper: off-load hints file I/O to a thread."""
    await asyncio.to_thread(_persist_hints_sync)


_load_hints_from_disk()


async def get_cached_hints(word: str, category: str) -> list[str] | None:
    """Return cached hints for this word/category pair, or None on miss."""
    key = _hints_key(word, category)
    value = _hints_store.get(key)
    if value is None:
        return None
    _hints_store.move_to_end(key)
    try:
        return json.loads(value)
    except Exception as exc:
        logger.debug("Hints cache deserialise failed (%r): %s", word, exc)
        _hints_store.pop(key, None)
        return None


async def cache_hints(word: str, category: str, hints: list[str]) -> None:
    """Store hints in the in-process LRU cache and write through to disk."""
    key = _hints_key(word, category)
    if key not in _hints_store and len(_hints_store) >= _MAX_HINTS:
        _hints_store.popitem(last=False)
    _hints_store[key] = json.dumps(hints, ensure_ascii=False)
    _hints_store.move_to_end(key)
    await _persist_hints()


# ── Word-category resolution cache ────────────────────────────────────────────
# Avoids repeated LLM calls to classify the same custom word into a category.
# E.g. if a game has been played with "самолёт" many times, we remember it is
# classified as "Транспорт" without hitting the LLM again.

_cat_store: OrderedDict[str, str] = OrderedDict()


def _cat_key(word: str) -> str:
    return word.lower().strip()


def _load_cat_from_disk() -> None:
    if not _CAT_CACHE_PATH.exists():
        return
    try:
        raw: dict[str, str] = json.loads(_CAT_CACHE_PATH.read_text(encoding="utf-8"))
        _cat_store.update(raw)
        while len(_cat_store) > _MAX_CAT:
            _cat_store.popitem(last=False)
        logger.debug("Category cache loaded: %d entries", len(_cat_store))
    except Exception as exc:
        logger.warning("Category cache load failed: %s — starting empty", exc)


def _persist_cat_sync() -> None:
    """Write _cat_store to disk. Runs inside to_thread."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CAT_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(dict(_cat_store), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CAT_CACHE_PATH)
    except Exception as exc:
        logger.debug("Category cache persist failed: %s", exc)


async def _persist_cat() -> None:
    await asyncio.to_thread(_persist_cat_sync)


_load_cat_from_disk()


async def get_cached_word_category(word: str) -> str | None:
    """Return the cached category for a custom word, or None on miss."""
    key = _cat_key(word)
    value = _cat_store.get(key)
    if value is None:
        return None
    _cat_store.move_to_end(key)
    return value


async def cache_word_category(word: str, category: str) -> None:
    """Persist the resolved category for a custom word."""
    key = _cat_key(word)
    if key not in _cat_store and len(_cat_store) >= _MAX_CAT:
        _cat_store.popitem(last=False)
    _cat_store[key] = category
    _cat_store.move_to_end(key)
    await _persist_cat()

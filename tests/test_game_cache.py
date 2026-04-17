# tests/test_game_cache.py
"""Unit tests for app/games/judgement_cache.py — the local-file LRU cache.

All tests run offline.  We isolate the in-process stores (_store, _hints_store)
by clearing them in setup/teardown so file I/O is never exercised in CI.
_persist() and _persist_hints() are patched to no-ops to avoid touching disk.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.games.judge import GuessJudgement
from app.games.judgement_cache import (
    _hints_key,
    _hints_store,
    _make_key,
    _store,
    cache_hints,
    cache_judgement,
    get_cached_hints,
    get_cached_judgement,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_COLD = GuessJudgement(status="cold", score=0.1, hint="Попробуй ещё")
_WARM = GuessJudgement(status="warm", score=0.5, hint="Теплее!")
_HOT = GuessJudgement(status="hot", score=0.9, hint="Горячо!")

_no_persist = patch("app.games.judgement_cache._persist", return_value=None)
_no_persist_hints = patch("app.games.judgement_cache._persist_hints", return_value=None)


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Clear in-process LRU stores before and after every test."""
    _store.clear()
    _hints_store.clear()
    yield
    _store.clear()
    _hints_store.clear()


# ── _make_key ─────────────────────────────────────────────────────────────────


class TestMakeKey:
    def test_deterministic(self):
        assert _make_key("Крокодил", "Аллигатор") == _make_key("Крокодил", "Аллигатор")

    def test_normalises_case_and_whitespace(self):
        assert _make_key("  Крокодил  ", "аллигатор") == _make_key("крокодил", "Аллигатор")

    def test_different_pairs_produce_different_keys(self):
        assert _make_key("кот", "собака") != _make_key("собака", "кот")

    def test_format_is_colon_separated(self):
        key = _make_key("слон", "носорог")
        assert key == "слон:носорог"


# ── _hints_key ────────────────────────────────────────────────────────────────


class TestHintsKey:
    def test_normalises(self):
        assert _hints_key("  Слон  ", "Животные") == _hints_key("слон", "животные")

    def test_separator_is_null_byte(self):
        # Keys for different words must never collide even if concatenated naively
        assert _hints_key("слон", "животные") != _hints_key("слонживотные", "")


# ── Judgement cache round-trip ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestJudgementCacheRoundTrip:
    async def test_store_and_retrieve(self):
        with _no_persist:
            await cache_judgement("крокодил", "аллигатор", _WARM)
            result = await get_cached_judgement("крокодил", "аллигатор")

        assert result is not None
        assert result.status == "warm"
        # NOTE: the cache module returns the deserialized object with cached=False;
        # the `cached=True` flag is set by judge_guess() in judge.py, not here.
        assert result.cached is False

    async def test_case_insensitive_retrieval(self):
        """Cache lookup must normalise keys, so case differences hit the same entry."""
        with _no_persist:
            await cache_judgement("Крокодил", "Аллигатор", _HOT)
            result = await get_cached_judgement("крокодил", "аллигатор")

        assert result is not None
        assert result.status == "hot"

    async def test_miss_returns_none(self):
        result = await get_cached_judgement("кот", "собака")
        assert result is None

    async def test_corrupt_entry_removed_and_returns_none(self):
        """A corrupt JSON value in _store must be removed and return None (not raise)."""
        key = _make_key("крокодил", "аллигатор")
        _store[key] = "{{not-valid-json}}"

        result = await get_cached_judgement("крокодил", "аллигатор")
        assert result is None
        assert key not in _store  # entry cleaned up

    async def test_lru_eviction_at_capacity(self):
        """When _store is at _MAX_ENTRIES, inserting a new entry evicts the oldest.

        Implementation detail: eviction triggers when len >= _MAX_ENTRIES, so
        we must fill to exactly _MAX_ENTRIES before the overflow entry.
        """
        from app.games.judgement_cache import _MAX_ENTRIES

        with _no_persist:
            # Fill to exact capacity so next insert triggers eviction
            for i in range(_MAX_ENTRIES):
                _store[f"dummy-key-{i:06d}"] = _COLD.model_dump_json()

            assert len(_store) == _MAX_ENTRIES
            first_key = "dummy-key-000000"
            assert first_key in _store

            # One more real entry → evict oldest (dummy-key-000000)
            await cache_judgement("слон", "носорог", _COLD)

            assert len(_store) == _MAX_ENTRIES
            # Oldest entry was evicted
            assert first_key not in _store


# ── Hints cache round-trip ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHintsCacheRoundTrip:
    async def test_store_and_retrieve(self):
        hints = ["Подсказка 1", "Подсказка 2", "Подсказка 3"]
        with _no_persist_hints:
            await cache_hints("слон", "Животные", hints)
            result = await get_cached_hints("слон", "Животные")

        assert result == hints

    async def test_case_insensitive_retrieval(self):
        hints = ["A", "B", "C"]
        with _no_persist_hints:
            await cache_hints("Слон", "Животные", hints)
            result = await get_cached_hints("слон", "животные")

        assert result == hints

    async def test_miss_returns_none(self):
        result = await get_cached_hints("кот", "Животные")
        assert result is None

    async def test_corrupt_entry_returns_none(self):
        key = _hints_key("слон", "животные")
        _hints_store[key] = "not-valid-json"

        result = await get_cached_hints("слон", "Животные")
        assert result is None
        assert key not in _hints_store

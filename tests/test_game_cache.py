# tests/test_game_cache.py
"""Unit tests for app/games/judgement_cache.py — the local-file LRU caches.

All tests run offline.  We isolate the in-process stores by clearing them in
setup/teardown so file I/O is never exercised in CI. Persist helpers are
patched to no-ops to avoid touching disk.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app.games.judgement_cache as judgement_cache_module
from app.games.judge import GuessJudgement
from app.games.judgement_cache import (
    _cat_store,
    _generated_words_key,
    _generated_words_store,
    _hints_key,
    _hints_store,
    _make_key,
    _make_key_v2,
    _store,
    cache_generated_words,
    cache_hints,
    cache_judgement,
    cache_word_category,
    get_cached_generated_words,
    get_cached_hints,
    get_cached_judgement,
    get_cached_word_category,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_COLD = GuessJudgement(status="cold", score=0.1, hint="Попробуй ещё")
_WARM = GuessJudgement(status="warm", score=0.5, hint="Теплее!")
_HOT = GuessJudgement(status="hot", score=0.9, hint="Горячо!")

_no_persist = patch("app.games.judgement_cache._persist", return_value=None)
_no_persist_hints = patch("app.games.judgement_cache._persist_hints", return_value=None)
_no_persist_cat = patch("app.games.judgement_cache._persist_cat", return_value=None)
_no_persist_generated_words = patch("app.games.judgement_cache._persist_generated_words", return_value=None)


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Clear in-process LRU stores before and after every test."""
    _store.clear()
    _hints_store.clear()
    _cat_store.clear()
    _generated_words_store.clear()
    yield
    _store.clear()
    _hints_store.clear()
    _cat_store.clear()
    _generated_words_store.clear()


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

    def test_make_key_v2_includes_topic_context(self):
        key1 = _make_key_v2("слон", "носорог", topic_id="topic:a", category="животные")
        key2 = _make_key_v2("слон", "носорог", topic_id="topic:b", category="животные")
        assert key1 != key2


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

    async def test_topic_context_isolated(self):
        with _no_persist:
            await cache_judgement("ноктюрн", "музыка", _COLD, topic_id="topic:music", category="музыка")
            same = await get_cached_judgement("ноктюрн", "музыка", topic_id="topic:music", category="музыка")
            other = await get_cached_judgement("ноктюрн", "музыка", topic_id="topic:lol", category="персонажи")

        assert same is not None
        assert other is None


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

    async def test_topic_context_isolated(self):
        hints = ["A", "B", "C"]
        with _no_persist_hints:
            await cache_hints("слон", "Животные", hints, topic_id="topic:animals")
            same = await get_cached_hints("слон", "Животные", topic_id="topic:animals")
            other = await get_cached_hints("слон", "Животные", topic_id="topic:other")
        assert same == hints
        assert other is None

    async def test_same_topic_id_shares_hints_across_category_variants(self):
        hints = ["A", "B", "C"]
        with _no_persist_hints:
            await cache_hints("райден", "Персонаж Genshin Impact", hints, topic_id="custom:ru:abc")
            same = await get_cached_hints("райден", "Персонаж   genshin impact!!!", topic_id="custom:ru:abc")
        assert same == hints


@pytest.mark.asyncio
class TestCategoryCacheRoundTrip:
    async def test_store_and_retrieve(self):
        with _no_persist_cat:
            await cache_word_category("Венти", "Персонажи")
            result = await get_cached_word_category("венти")

        assert result == "Персонажи"

    async def test_miss_returns_none(self):
        result = await get_cached_word_category("несуществующее-слово")
        assert result is None


@pytest.mark.asyncio
class TestGeneratedWordsCacheRoundTrip:
    async def test_store_and_retrieve(self):
        words = ["венти", "чжун ли", "нахида"]
        with _no_persist_generated_words:
            await cache_generated_words("ru", "Персонаж Genshin Impact", words)
            result = await get_cached_generated_words("ru", "персонаж genshin impact")

        assert result == words

    async def test_key_is_case_insensitive(self):
        assert _generated_words_key("RU", " Персонаж Genshin Impact ") == _generated_words_key(
            "ru",
            "персонаж genshin impact",
        )

    async def test_corrupt_entry_returns_none(self):
        key = _generated_words_key("ru", "персонаж genshin impact")
        _generated_words_store[key] = '{"oops": "not-a-list"}'

        result = await get_cached_generated_words("ru", "Персонаж Genshin Impact")
        assert result is None
        assert key not in _generated_words_store

    async def test_topic_context_isolated(self):
        words = ["венти", "чжун ли", "нахида"]
        with _no_persist_generated_words:
            await cache_generated_words("ru", "персонаж genshin impact", words, topic_id="topic:a")
            same = await get_cached_generated_words("ru", "персонаж genshin impact", topic_id="topic:a")
            other = await get_cached_generated_words("ru", "персонаж genshin impact", topic_id="topic:b")
        assert same == words
        assert other is None

    async def test_same_topic_id_shares_generated_words_across_category_variants(self):
        words = ["венти", "чжун ли", "нахида"]
        with _no_persist_generated_words:
            await cache_generated_words("ru", "Персонаж Genshin Impact", words, topic_id="custom:ru:abc")
            same = await get_cached_generated_words("ru", "Персонаж   genshin impact!!!", topic_id="custom:ru:abc")
        assert same == words

    async def test_topic_scoped_generated_words_survive_restart_and_ignore_other_topic(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "generated_words_cache.json"
        monkeypatch.setattr(judgement_cache_module, "_GEN_WORDS_CACHE_PATH", cache_path)
        _generated_words_store.clear()
        words = ["венти", "чжун ли", "нахида"]

        await cache_generated_words("ru", "Персонаж Genshin Impact", words, topic_id="custom:ru:abc")
        _generated_words_store.clear()
        judgement_cache_module._load_generated_words_from_disk()

        same = await get_cached_generated_words("ru", "Персонаж   genshin impact!!!", topic_id="custom:ru:abc")
        other = await get_cached_generated_words("ru", "персонаж honkai star rail", topic_id="custom:ru:def")

        assert same == words
        assert other is None

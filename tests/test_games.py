# tests/test_games.py
"""Unit tests for app/games/ — Crocodile game subsystem.

All tests run fully offline:
  - No Redis required (in-memory fallback is exercised by save/load).
  - No LLM calls (judge pipeline is mocked at _race_generate level;
    the local Levenshtein path is exercised directly).
  - No Telegram Bot API.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.games.crocodile import CrocodileGame, create_game, load_game
from app.games.judge import (
    GuessJudgement,
    _allowed_edits,
    _damerau_levenshtein,
    _local_check,
)
from app.games.word_bank import (
    _detect_lang,
    list_categories,
    pick_random_word,
    resolve_category,
    resolve_topic,
    validate_custom_word,
)

# ── word_bank ─────────────────────────────────────────────────────────────────


class TestResolveCategory:
    def test_exact_russian(self):
        lang, cat = resolve_category("животные")
        assert lang == "ru"
        assert cat == "Животные"

    def test_exact_english(self):
        lang, cat = resolve_category("animals")
        assert lang == "en"
        assert cat == "Animals"

    def test_case_insensitive(self):
        result = resolve_category("ЖИВОТНЫЕ")
        assert result is not None
        assert result[1] == "Животные"

    def test_prefix_match(self):
        result = resolve_category("жив")  # ≥3 chars prefix
        assert result is not None
        assert result[1] == "Животные"

    def test_too_short_prefix_no_match(self):
        # 2-char prefix must NOT match (guard in resolve_category requires ≥3)
        result = resolve_category("жи")
        assert result is None

    def test_unknown_returns_none(self):
        assert resolve_category("xyz_unknown_category_99") is None

    def test_alias_кино(self):
        lang, cat = resolve_category("кино")
        assert lang == "ru"
        assert cat == "Фильмы"


class TestListCategories:
    def test_ru_categories(self):
        cats = list_categories("ru")
        assert "Животные" in cats
        assert "Еда" in cats

    def test_en_categories(self):
        cats = list_categories("en")
        assert "Animals" in cats
        assert "Food" in cats


class TestResolveTopic:
    def test_similar_lol_topics_resolve_to_same_topic_id(self):
        t1 = resolve_topic("герой League of Legends")
        t2 = resolve_topic("герои Лиги Легенд")

        assert t1.topic_id == t2.topic_id
        assert t1.topic_id == "special:lol_champions"
        assert "League of Legends" in t1.category

    def test_custom_topic_uses_stable_hash_id(self):
        t1 = resolve_topic("персонаж genshin impact")
        t2 = resolve_topic("Персонаж   genshin impact!!!")
        assert t1.topic_id == t2.topic_id
        assert t1.topic_id.startswith("custom:ru:")


class TestValidateCustomWord:
    def test_valid_russian(self):
        assert validate_custom_word("крокодил") == "крокодил"

    def test_valid_english(self):
        assert validate_custom_word("Elephant") == "elephant"

    def test_strips_whitespace(self):
        assert validate_custom_word("  жираф  ") == "жираф"

    def test_too_short_rejected(self):
        assert validate_custom_word("я") is None

    def test_too_long_rejected(self):
        assert validate_custom_word("а" * 41) is None

    def test_special_chars_rejected(self):
        assert validate_custom_word("кот@собака") is None

    def test_hyphen_allowed(self):
        assert validate_custom_word("северный-олень") == "северный-олень"


@pytest.mark.asyncio
class TestPickRandomWord:
    @pytest.fixture(autouse=True)
    def mock_word_gen(self):
        with (
            patch(
                "app.games.word_bank._generate_single_word_fast",
                new_callable=AsyncMock,
                return_value=None,  # force fallthrough to generate_words_for_category
            ),
            patch(
                "app.games.word_bank.generate_words_for_category",
                new_callable=AsyncMock,
                return_value=["моксЛово", "тестовое"],
            ),
        ):
            yield

    async def test_returns_word_from_category(self):
        word, lang, cat, is_gen = await pick_random_word("животные")
        assert lang == "ru"
        assert cat == "Животные"
        assert isinstance(word, str)
        assert len(word) > 0
        assert not is_gen

    async def test_unknown_category_returns_something(self):
        word, lang, cat, is_gen = await pick_random_word("totally_unknown_xyz")
        assert isinstance(word, str)
        assert lang in ("ru", "en")
        assert is_gen
        assert word in ("моксЛово", "тестовое")

    async def test_english_category(self):
        word, lang, cat, is_gen = await pick_random_word("animals")
        assert lang == "en"
        assert cat == "Animals"
        assert not is_gen

    async def test_unknown_category_uses_persisted_generated_words_before_llm(self):
        cached_words = ["венти", "чжун ли", "нахида"]
        with (
            patch(
                "app.games.word_bank._generate_single_word_fast",
                new_callable=AsyncMock,
            ) as fast_mock,
            patch(
                "app.games.word_bank.generate_words_for_category",
                new_callable=AsyncMock,
            ) as generate_mock,
            patch(
                "app.games.judgement_cache.get_cached_generated_words",
                new_callable=AsyncMock,
                return_value=cached_words,
            ) as cache_mock,
        ):
            word, lang, cat, is_gen = await pick_random_word("персонаж genshin impact")

        assert lang == "ru"
        assert cat == "персонаж genshin impact"
        assert is_gen
        assert word in cached_words
        assert cache_mock.await_count == 1
        cache_call = cache_mock.await_args
        assert cache_call.args == ("ru", "персонаж genshin impact")
        assert cache_call.kwargs.get("topic_id", "").startswith("custom:ru:")
        fast_mock.assert_not_awaited()
        generate_mock.assert_not_awaited()

    async def test_same_topic_returns_different_words_across_sequential_calls(self):
        first_word, *_ = await pick_random_word("animals")
        second_word, *_ = await pick_random_word("animals")
        assert first_word != second_word


# ── judge — Damerau-Levenshtein algorithm ─────────────────────────────────────


class TestDamerauLevenshtein:
    """Verify the string distance metric covers all four edit operations."""

    def test_equal_strings(self):
        assert _damerau_levenshtein("кот", "кот") == 0

    def test_substitution(self):
        assert _damerau_levenshtein("кот", "кит") == 1

    def test_transposition_adjacent(self):
        # Damerau: adjacent swap counts as 1, not 2
        assert _damerau_levenshtein("кот", "кто") == 1

    def test_insertion(self):
        assert _damerau_levenshtein("кот", "крот") == 1

    def test_deletion(self):
        assert _damerau_levenshtein("крот", "кот") == 1

    def test_multi_edit(self):
        # completely different words of same length
        assert _damerau_levenshtein("слон", "кит") >= 3

    def test_empty_strings(self):
        assert _damerau_levenshtein("", "") == 0
        assert _damerau_levenshtein("кот", "") == 3
        assert _damerau_levenshtein("", "кот") == 3


class TestAllowedEdits:
    """Verify tolerance thresholds by word length."""

    def test_very_short_zero_edits(self):
        assert _allowed_edits(1) == 0
        assert _allowed_edits(3) == 0
        assert _allowed_edits(4) == 0

    def test_medium_one_edit(self):
        assert _allowed_edits(5) == 1
        assert _allowed_edits(6) == 1
        assert _allowed_edits(7) == 1

    def test_long_two_edits(self):
        assert _allowed_edits(8) == 2
        assert _allowed_edits(12) == 2
        assert _allowed_edits(20) == 2


class TestLocalCheck:
    """verify _local_check using length-dependent Damerau-Levenshtein."""

    def test_exact_match(self):
        assert _local_check("крокодил", "крокодил") == "exact_match"

    def test_case_insensitive(self):
        assert _local_check("Крокодил", "крокодил") == "exact_match"

    def test_mongust_regression(self):
        """The core regression: монгуст → мангуст (7 chars, 1 edit, allowed=1)."""
        assert _local_check("мангуст", "монгуст") == "exact_match"

    def test_long_word_two_edit_tolerance(self):
        # "крокодил" (8 chars) → _allowed_edits(8)=2; "крокадил" dist=1 → match
        assert _local_check("крокодил", "крокадил") == "exact_match"

    def test_long_word_double_typo(self):
        # "крокодил" → "крокадалл" dist=3 → no match (>2)
        assert _local_check("крокодил", "крокадалл") is None

    def test_extra_char_long_word(self):
        # "крокодилл" (insertion, dist=1) on 8-char word → match
        assert _local_check("крокодил", "крокодилл") == "exact_match"

    def test_short_word_zero_tolerance(self):
        # "кот" (3 chars) → _allowed_edits(3)=0; "кит" dist=1 → no match
        assert _local_check("кот", "кит") is None

    def test_4char_zero_tolerance(self):
        # "слон" (4 chars) → _allowed_edits(4)=0; "слан" dist=1 → no match
        assert _local_check("слон", "слан") is None

    def test_transposition_caught(self):
        # "кракодил" ↔ "крокодил": pos 2-3 transposition, dist=1, 8 chars, allowed=2
        assert _local_check("крокодил", "кракодил") == "exact_match"

    def test_different_word_no_match(self):
        assert _local_check("слон", "крокодил") is None

    def test_english_exact(self):
        assert _local_check("elephant", "elephant") == "exact_match"


# ── CrocodileGame state machine ───────────────────────────────────────────────

# Shared offline judgement stubs
_COLD = GuessJudgement(status="cold", score=0.1, hint="Попробуй ещё! ❄️")
_WARM = GuessJudgement(status="warm", score=0.55, hint="Теплее! 🌡️")


class _FakeRedis:
    def __init__(self):
        self.set_calls: list[tuple[str, int | None]] = []

    async def set(self, key, value, ex=None):
        self.set_calls.append((key, ex))

    async def get(self, key):
        return None

    async def delete(self, key):
        return None


class TestCrocodileGameSerialisation:
    def test_round_trip(self):
        game = CrocodileGame(
            game_id="test-uuid-1234",
            target_word="крокодил",
            category="Животные",
            lang="ru",
            inline_message_id="inl123",
            creator_id=42,
            guesser_id=None,
        )
        serialised = game.to_json()
        restored = CrocodileGame.from_json(serialised)
        assert restored.game_id == game.game_id
        assert restored.target_word == game.target_word
        assert restored.creator_id == game.creator_id
        assert restored.status == "active"

    def test_from_json_ignores_unknown_fields(self):
        """from_json must silently drop unexpected keys (injection defence)."""
        data = {
            "game_id": "abc",
            "target_word": "кот",
            "category": "Животные",
            "lang": "ru",
            "inline_message_id": "x",
            "creator_id": 1,
            "guesser_id": None,
            "attempts": [],
            "max_attempts": 10,
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "_injected_evil_field": "pwned",  # must be silently dropped
        }
        game = CrocodileGame.from_json(json.dumps(data))
        assert game.target_word == "кот"
        assert not hasattr(game, "_injected_evil_field")


@pytest.mark.asyncio
class TestCrocodileGameInMemory:
    """Run the game against the in-memory fallback (no Redis, no LLM)."""

    @pytest.fixture(autouse=True)
    def mock_llm(self):
        """Patch _race_generate so all non-local guesses return cold offline.

        This is the minimal mock needed: _local_check still fires first (real
        implementation), so exact / typo matches never reach _race_generate.
        Non-matching guesses get the _COLD stub, enabling game_over tests.
        _prefetch_hints is suppressed to avoid background LLM calls in CI.
        """
        with (
            patch("app.games.judge._race_generate", new_callable=AsyncMock, return_value=_COLD),
            patch("app.games.judgement_cache.cache_judgement", new_callable=AsyncMock),
            patch("app.games.judgement_cache.get_cached_judgement", new_callable=AsyncMock, return_value=None),
            patch("app.games.crocodile._prefetch_hints", new_callable=AsyncMock),
        ):
            yield

    async def test_create_and_load_roundtrip(self):
        game = await create_game(
            target_word="тест",
            category="Разное",
            lang="ru",
            inline_message_id="inline_test_1",
            creator_id=99,
        )
        loaded = await load_game(game.game_id)
        assert loaded is not None
        assert loaded.target_word == "тест"

    async def test_exact_guess_wins_game(self):
        game = await create_game(
            target_word="крокодил",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_2",
            creator_id=7,
        )
        event = await game.process_guess("крокодил")
        assert event["event"] == "result"
        assert event["status"] == "exact_match"
        assert game.status == "won"

    async def test_mongust_typo_regression(self):
        """'монгуст' must now win against 'мангуст' (7 chars, 1 edit, allowed=1)."""
        game = await create_game(
            target_word="мангуст",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_3a",
            creator_id=7,
        )
        event = await game.process_guess("монгуст")
        assert event["status"] == "exact_match", f"Expected exact_match for монгуст→мангуст, got {event['status']!r}"
        assert game.status == "won"

    async def test_krokadil_typo_exact_match(self):
        """'крокадил' must match 'крокодил' (8 chars, dist=1, allowed=2)."""
        game = await create_game(
            target_word="крокодил",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_3b",
            creator_id=7,
        )
        event = await game.process_guess("крокадил")
        assert event["status"] == "exact_match"
        assert game.status == "won"

    async def test_wrong_guess_counts_attempt(self):
        """Non-matching guess → cold result → attempt recorded."""
        game = await create_game(
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_4",
            creator_id=5,
        )
        event = await game.process_guess("кот")
        assert event["event"] == "result"
        assert event["status"] == "cold"
        assert event["attempts"] == 1
        assert game.status == "active"

    async def test_max_attempts_triggers_game_over(self):
        game = await create_game(
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_5",
            creator_id=5,
        )
        game.max_attempts = 2  # Reduce for speed

        await game.process_guess("кот")
        event = await game.process_guess("собака")

        assert event["event"] == "game_over"
        assert event["reason"] == "max_attempts"
        assert game.status == "lost"
        assert event["word"] == "слон"

    async def test_history_recorded_on_guess(self):
        """Each guess (non-unavailable) must be appended to _mem_history."""
        from app.games.crocodile import get_game_history

        game = await create_game(
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_6",
            creator_id=5,
        )
        await game.process_guess("кот")
        await game.process_guess("собака")

        history = get_game_history(game.game_id)
        assert len(history) == 2
        assert history[0]["word"] == "кот"
        assert history[0]["status"] == "cold"

    async def test_judge_unavailable_does_not_count_attempt(self):
        """judge_unavailable events must NOT count the attempt."""
        with patch(
            "app.games.judge._race_generate",
            new_callable=AsyncMock,
            return_value=None,  # simulate total LLM failure
        ):
            game = await create_game(
                target_word="слон",
                category="Животные",
                lang="ru",
                inline_message_id="inline_test_7",
                creator_id=5,
            )
            event = await game.process_guess("кот")
            assert event["event"] == "judge_unavailable"
            assert len(game.attempts) == 0  # NOT counted
            assert game.status == "active"

    async def test_create_game_uses_idle_ttl_in_redis(self):
        fake_redis = _FakeRedis()
        with patch("app.cache.redis_client", fake_redis):
            await create_game(
                target_word="слон",
                category="Животные",
                lang="ru",
                inline_message_id="inline_ttl_idle",
                creator_id=5,
            )

        assert fake_redis.set_calls
        assert fake_redis.set_calls[-1][1] == 1209600

    async def test_successful_guess_refreshes_active_ttl_in_redis(self):
        fake_redis = _FakeRedis()
        with patch("app.cache.redis_client", fake_redis):
            game = await create_game(
                target_word="слон",
                category="Животные",
                lang="ru",
                inline_message_id="inline_ttl_active",
                creator_id=5,
            )
            game.guesser_id = 123  # Simulate guesser joining via WebSocket
            await game.process_guess("кот")

        assert fake_redis.set_calls[0][1] == 1209600
        assert fake_redis.set_calls[-1][1] == 172800

    async def test_judge_unavailable_refreshes_active_ttl_without_counting_attempt(self):
        fake_redis = _FakeRedis()
        with (
            patch("app.cache.redis_client", fake_redis),
            patch(
                "app.games.judge._race_generate",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            game = await create_game(
                target_word="слон",
                category="Животные",
                lang="ru",
                inline_message_id="inline_ttl_unavailable",
                creator_id=5,
            )
            game.guesser_id = 123  # Simulate guesser joining via WebSocket
            event = await game.process_guess("кот")

        assert event["event"] == "judge_unavailable"
        assert len(game.attempts) == 0
        assert game.status == "active"
        assert fake_redis.set_calls[0][1] == 1209600
        assert fake_redis.set_calls[-1][1] == 172800
        assert len(fake_redis.set_calls) == 2

    async def test_empty_guess_returns_error(self):
        game = await create_game(
            target_word="тест",
            category="Разное",
            lang="ru",
            inline_message_id="inline_test_8",
            creator_id=3,
        )
        event = await game.process_guess("   ")
        assert event["event"] == "error"

    async def test_game_not_found_returns_none(self):
        result = await load_game("nonexistent-game-id-xyz")
        assert result is None

    async def test_process_guess_on_finished_game_still_works(self):
        """process_guess on a won game must still return a result (caller owns the break)."""
        game = await create_game(
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_9",
            creator_id=5,
        )
        # Win the game first
        await game.process_guess("слон")
        assert game.status == "won"

        # Call again — should return error (empty guard) or a valid event;
        # the critical invariant is: does NOT raise an unhandled exception.
        event2 = await game.process_guess("слон")
        # After win, attempts already appended; process_guess returns result event
        assert "event" in event2


# ── _detect_lang ──────────────────────────────────────────────────────────────


class TestDetectLang:
    """_detect_lang heuristic: Cyrillic ratio > 0.3 → ru, else en."""

    def test_pure_cyrillic(self):
        assert _detect_lang("крокодил") == "ru"

    def test_pure_latin(self):
        assert _detect_lang("crocodile") == "en"

    def test_empty_string_defaults_ru(self):
        # Empty → ratio calculation skipped → return "ru"
        assert _detect_lang("") == "ru"

    def test_mixed_majority_cyrillic(self):
        # "кот dog" — 3 Cyrillic of 7 chars = 0.43 > 0.3 → ru
        assert _detect_lang("кот dog") == "ru"

    def test_mixed_majority_latin(self):
        # One Cyrillic letter among mostly latin letters should stay "en".
        assert _detect_lang("abcd e f g к hijk") == "en"


# ── validate_custom_word boundary cases ───────────────────────────────────────


class TestValidateCustomWordBoundaries:
    """Boundary cases not covered by the original 7 tests."""

    def test_exactly_2_chars_valid(self):
        assert validate_custom_word("яя") == "яя"

    def test_exactly_40_chars_valid(self):
        assert validate_custom_word("а" * 40) == "а" * 40

    def test_41_chars_rejected(self):
        assert validate_custom_word("а" * 41) is None

    def test_space_inside_word_allowed(self):
        # Multi-word phrases like "северное сияние" are in the word bank
        assert validate_custom_word("северное сияние") == "северное сияние"


# ── CrocodileGame serialisation edge cases ─────────────────────────────────────


class TestCrocodileGameSerialisationEdgeCases:
    def test_from_json_accepts_bytes(self):
        """from_json must accept bytes (Redis returns bytes from .get())."""
        game = CrocodileGame(
            game_id="bytes-test",
            target_word="кот",
            category="Животные",
            lang="ru",
            inline_message_id="inl1",
            creator_id=1,
            guesser_id=None,
        )
        raw_bytes: bytes = game.to_json().encode("utf-8")
        restored = CrocodileGame.from_json(raw_bytes)
        assert restored.game_id == "bytes-test"
        assert restored.target_word == "кот"

    def test_from_json_malformed_raises(self):
        """Malformed JSON must raise, not silently return garbage."""
        import pytest

        with pytest.raises(Exception):
            CrocodileGame.from_json(b"not-json-at-all")


# ── In-memory LRU bounded store ───────────────────────────────────────────────


class TestMemoryFallbackBounded:
    """_mem_put must evict oldest entry when _MEM_MAX is reached."""

    def test_lru_evicts_oldest_at_capacity(self):
        from app.games.crocodile import _MEM_MAX, _mem_games, _mem_put
        from tests.factories import make_crocodile_game

        # _mem_games is cleared by the autouse fixture between tests
        assert len(_mem_games) == 0

        # Fill to capacity
        for i in range(_MEM_MAX):
            _mem_put(make_crocodile_game(game_id=f"game-{i:04d}"))

        assert len(_mem_games) == _MEM_MAX
        first_key = "game-0000"
        assert first_key in _mem_games

        # One more → evicts oldest (game-0000)
        _mem_put(make_crocodile_game(game_id="game-overflow"))
        assert len(_mem_games) == _MEM_MAX
        assert first_key not in _mem_games
        assert "game-overflow" in _mem_games

    def test_get_missing_returns_none(self):
        from app.games.crocodile import _mem_get

        assert _mem_get("does-not-exist") is None


# ── In-memory getters ────────────────────────────────────────────────────────


class TestGameAccessors:
    """U-05: get_game_hints() / get_game_history()"""

    def test_get_game_hints_returns_empty_by_default(self):
        from app.games.crocodile import get_game_hints

        assert get_game_hints("unknown-id") == []

    def test_get_game_hints_returns_list(self):
        from app.games.crocodile import _mem_hints, get_game_hints

        _mem_hints["test-id"] = ["Hint 1", "Hint 2"]
        assert get_game_hints("test-id") == ["Hint 1", "Hint 2"]

    def test_get_game_history_returns_empty_by_default(self):
        from app.games.crocodile import get_game_history

        assert get_game_history("unknown-id") == []

    def test_get_game_history_returns_events(self):
        from app.games.crocodile import _mem_history, get_game_history

        _mem_history["test-id"] = [{"event": "guess", "guess": "кот"}]
        assert get_game_history("test-id") == [{"event": "guess", "guess": "кот"}]

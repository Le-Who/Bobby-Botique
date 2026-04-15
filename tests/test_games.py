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
    list_categories,
    pick_random_word,
    resolve_category,
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
        with patch("app.games.word_bank.generate_words_for_category", new_callable=AsyncMock, return_value=["моксЛово", "тестовое"]):
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
        assert event["status"] == "exact_match", (
            f"Expected exact_match for монгуст→мангуст, got {event['status']!r}"
        )
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

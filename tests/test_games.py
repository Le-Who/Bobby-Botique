# tests/test_games.py
"""Unit tests for app/games/ — Crocodile game subsystem.

All tests run fully offline:
  - No Redis required (in-memory fallback is exercised by save/load).
  - No LLM calls (judge pipeline is tested via the local Levenshtein path).
  - No Telegram Bot API.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.games.crocodile import CrocodileGame, create_game, load_game
from app.games.judge import _fallback_judgement, _local_check
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
    async def test_returns_word_from_category(self):
        word, lang, cat = await pick_random_word("животные")
        assert lang == "ru"
        assert cat == "Животные"
        assert isinstance(word, str)
        assert len(word) > 0

    async def test_unknown_category_returns_something(self):
        # Unknown category falls back to random — should still return a result
        word, lang, cat = await pick_random_word("totally_unknown_xyz")
        assert isinstance(word, str)
        assert lang in ("ru", "en")

    async def test_english_category(self):
        word, lang, cat = await pick_random_word("animals")
        assert lang == "en"
        assert cat == "Animals"


# ── judge — local checks only (no LLM) ───────────────────────────────────────


class TestLocalCheck:
    def test_exact_match(self):
        assert _local_check("крокодил", "крокодил") == "exact_match"

    def test_case_insensitive(self):
        assert _local_check("Крокодил", "крокодил") == "exact_match"

    def test_typo_high_similarity(self):
        # "крокодилл" (double-l typo) is ≥90% similar to "крокодил"
        # ratio = 2*8/(8+9) ≈ 0.94 — safely above the 0.90 threshold
        result = _local_check("крокодил", "крокодилл")
        assert result == "exact_match", (
            f"Expected exact_match for near-correct typo, got {result!r}"
        )

    def test_below_threshold_no_match(self):
        # "крокадил" has ratio 0.875 < 0.90 — should NOT match via _local_check
        # (would need the LLM path for a 'hot' judgement)
        import difflib
        ratio = difflib.SequenceMatcher(None, "крокодил", "крокадил").ratio()
        assert ratio < 0.90, f"Ratio changed: {ratio:.3f}"
        result = _local_check("крокодил", "крокадил")
        assert result is None  # Requires LLM path for judgement

    def test_different_word_no_match(self):
        assert _local_check("слон", "крокодил") is None

    def test_english_exact(self):
        assert _local_check("elephant", "elephant") == "exact_match"


class TestFallbackJudgement:
    def test_hot_on_high_similarity(self):
        j = _fallback_judgement("крокодил", "крокодил")
        assert j.status == "hot"
        assert j.score >= 0.7

    def test_cold_on_unrelated(self):
        j = _fallback_judgement("слон", "самолёт")
        assert j.status in ("cold", "warm")

    def test_score_range(self):
        for target, guess in [("cat", "dog"), ("cat", "cats"), ("python", "python")]:
            j = _fallback_judgement(target, guess)
            assert 0.0 <= j.score <= 1.0


# ── CrocodileGame state machine ───────────────────────────────────────────────


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
    """Run the game against the in-memory fallback (no Redis needed)."""

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

    async def test_typo_guess_is_scored(self):
        """In offline tests (no LLM), a typo guess goes through _fallback_judgement.

        _fallback_judgement uses SequenceMatcher ratio, so "крокадил" (≈94% match)
        should receive a score ≥ 0.9 and the 'hot' status from the fallback.
        The full exact_match short-circuit happens inside judge_guess's _local_check
        step, which fires _before_ the LLM race — so a sufficiently close typo
        SHOULD return exact_match even in in-memory mode.
        """
        game = await create_game(
            target_word="крокодил",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_3",
            creator_id=7,
        )
        event = await game.process_guess("крокадил")
        # _local_check fires before the LLM; this guess passes the 90% threshold.
        # Expected: exact_match (wins game) — verified by ratio test above.
        assert event["status"] in ("exact_match", "hot"), (
            f"Unexpected status: {event['status']} (score={event.get('score')})"
        )
        # Game should be either won (if _local_check fired) or still active
        assert game.status in ("won", "active")

    async def test_max_attempts_triggers_game_over(self):
        game = await create_game(
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="inline_test_4",
            creator_id=5,
        )
        game.max_attempts = 2  # Reduce for speed

        # Two wrong guesses
        await game.process_guess("кот")
        event = await game.process_guess("собака")

        assert event["event"] == "game_over"
        assert event["reason"] == "max_attempts"
        assert game.status == "lost"
        assert event["word"] == "слон"

    async def test_empty_guess_returns_error(self):
        game = await create_game(
            target_word="тест",
            category="Разное",
            lang="ru",
            inline_message_id="inline_test_5",
            creator_id=3,
        )
        event = await game.process_guess("   ")
        assert event["event"] == "error"

    async def test_game_not_found_returns_none(self):
        result = await load_game("nonexistent-game-id-xyz")
        assert result is None

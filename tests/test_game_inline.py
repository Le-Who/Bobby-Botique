# tests/test_game_inline.py
"""Tests for the inline Crocodile game initialization logic.

Tests the regex used to trigger the game and the `_init_croc_game_async` setup logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.inline import _CROC_PREFIX_RE, _init_croc_game_async

# ── _CROC_PREFIX_RE ──────────────────────────────────────────────────────────


class TestCrocPrefixRegex:
    """U-11: Make sure the inline trigger matches correctly."""

    @pytest.mark.parametrize(
        "query, expected_prefix, expected_arg",
        [
            ("крокодил: животные", "крокодил: ", "животные"),
            ("крок:=слон", "крок:", "=слон"),
            ("croc:   ", "croc:   ", ""),
            ("crocodile: random", "crocodile: ", "random"),
            ("Крок:Разное", "Крок:", "Разное"),
        ],
    )
    def test_matches_valid_prefixes(self, query: str, expected_prefix: str, expected_arg: str):
        match = _CROC_PREFIX_RE.match(query)
        assert match is not None
        assert match.group(0) == expected_prefix
        # Removing the matched prefix should leave the argument
        assert _CROC_PREFIX_RE.sub("", query).strip() == expected_arg.strip()

    @pytest.mark.parametrize(
        "query",
        [
            "прокодил:",
            "крокодил",
            "крокодильчик:",
            "crocc:",
            "крокодил : животные",  # normal space before colon is not matched
        ],
    )
    def test_fails_on_invalid_prefixes(self, query: str):
        assert _CROC_PREFIX_RE.match(query) is None


# ── _init_croc_game_async ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestInitCrocGameAsync:
    """I-04: Integration logic for inline game creation."""

    @pytest.fixture
    def mock_bot(self):
        bot = AsyncMock()
        bot.username = "testbot"
        return bot

    async def test_category_mode_success(self, mock_bot):
        """Standard category mode picks a word and creates a game."""
        with (
            patch("app.games.word_bank.pick_random_word", new_callable=AsyncMock) as pick_mock,
            patch("app.games.crocodile.create_game", new_callable=AsyncMock) as create_mock,
        ):
            pick_mock.return_value = ("жираф", "ru", "Животные", False)
            create_mock.return_value = AsyncMock(game_id="game-1")

            await _init_croc_game_async(
                bot=mock_bot,
                inline_message_id="msg-1",
                arg="животные",
                creator_id=42,
            )

            pick_mock.assert_awaited_once_with("животные")
            create_mock.assert_awaited_once_with(
                target_word="жираф",
                category="Животные",
                lang="ru",
                inline_message_id="msg-1",
                creator_id=42,
            )
            # The inline keyboard gets updated
            mock_bot.edit_message_text.assert_awaited_once()
            call_kwargs = mock_bot.edit_message_text.await_args.kwargs
            assert "Слово загадано!" in call_kwargs["text"]

    async def test_unintelligible_category_shows_error(self, mock_bot):
        """ValueError from pick_random_word updates inline msg with error."""
        with (
            patch("app.games.word_bank.pick_random_word", new_callable=AsyncMock) as pick_mock,
            patch("app.games.crocodile.create_game", new_callable=AsyncMock) as create_mock,
        ):
            pick_mock.side_effect = ValueError("Unknown category")

            await _init_croc_game_async(
                bot=mock_bot,
                inline_message_id="msg-2",
                arg="asdfqwer",
                creator_id=42,
            )

            # Execution stops before creating game
            create_mock.assert_not_called()
            # Bot edits directly to show error text
            mock_bot.edit_message_text.assert_awaited_once()
            call_kwargs = mock_bot.edit_message_text.await_args.kwargs
            assert "Не могу понять тему" in call_kwargs["text"]

    async def test_custom_word_mode(self, mock_bot):
        """'=word' arg bypasses pick_random_word and creates a custom game with static category.

        Bug-6.3 fix: category is set to a static label — no extra LLM call,
        so the player is not waiting for category resolution.
        """
        with (
            patch("app.games.word_bank.pick_random_word", new_callable=AsyncMock) as pick_mock,
            patch("app.games.crocodile.create_game", new_callable=AsyncMock) as create_mock,
        ):
            create_mock.return_value = AsyncMock(game_id="game-2")

            await _init_croc_game_async(
                bot=mock_bot,
                inline_message_id="msg-3",
                arg="=секрет",
                creator_id=42,
            )

            pick_mock.assert_not_called()
            # Category is a static label — no LLM call needed
            create_mock.assert_awaited_once_with(
                target_word="секрет",
                category="Слово игрока (особое)",
                lang="ru",  # detected via cyrillic check
                inline_message_id="msg-3",
                creator_id=42,
            )

    async def test_custom_word_mode_bank_hit(self, mock_bot):
        """Custom word always gets a static category — no LLM delay, no bank lookup.

        Bug-6.4 redesign: category classification moved to background, not blocking.
        """
        with (
            patch("app.games.word_bank.pick_random_word", new_callable=AsyncMock) as pick_mock,
            patch("app.games.crocodile.create_game", new_callable=AsyncMock) as create_mock,
        ):
            create_mock.return_value = AsyncMock(game_id="game-bank")

            await _init_croc_game_async(
                bot=mock_bot,
                inline_message_id="msg-bank",
                arg="=крокодил",
                creator_id=99,
            )

            pick_mock.assert_not_called()
            # Category is always static — no waiting, game starts instantly
            create_mock.assert_awaited_once_with(
                target_word="крокодил",
                category="Слово игрока (особое)",
                lang="ru",
                inline_message_id="msg-bank",
                creator_id=99,
            )

    async def test_invalid_custom_word_shows_error(self, mock_bot):
        """Custom word violating 2-40 char limit updates message with error."""
        with (
            patch("app.games.word_bank.pick_random_word", new_callable=AsyncMock) as pick_mock,
            patch("app.games.crocodile.create_game", new_callable=AsyncMock) as create_mock,
        ):
            # Pass a 1-character custom word
            await _init_croc_game_async(
                bot=mock_bot,
                inline_message_id="msg-4",
                arg="=а",
                creator_id=42,
            )

            pick_mock.assert_not_called()
            create_mock.assert_not_called()
            mock_bot.edit_message_text.assert_awaited_once()
            call_kwargs = mock_bot.edit_message_text.await_args.kwargs
            assert "Недопустимое слово" in call_kwargs["text"]

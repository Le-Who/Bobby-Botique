"""
E2E tests for the Crocodile Game Engine.

Design decisions
────────────────
- Redis is replaced by the in-memory fallback (_mem_games/_mem_hints/_mem_history)
  so tests run without an external dependency — this is the canonical path the
  engine takes when redis_client is None.
- All LLM calls (judge_guess, generate_hints) are mocked at the boundary.
- Tests validate the full state machine lifecycle including:
    ・ Game creation and persistence
    ・ Guess processing with exact/warm/cold outcomes
    ・ Homoglyph normalization — Cyrillic typos within allowed DL distance
    ・ Judge unavailable sentinel — attempt NOT counted
    ・ Game termination (won / lost / surrender)
    ・ PubSub fan-out to subscribers

Coverage:
  CG-01  create_game stores game in memory and starts hint task
  CG-02  Exact match → status=won, word revealed
  CG-03  Warm/cold guess → recorded in history, attempts incremented
  CG-04  Cyrillic homoglyph typo within DL tolerance → exact_match
  CG-05  Judge unavailable — attempt NOT counted
  CG-06  Max attempts reached → status=lost
  CG-07  Surrender → status=lost, word revealed
  CG-08  PubSub broadcast reaches all subscribers except sender
  CG-09  load_game returns None for unknown game_id
  CG-10  Best score tracking across multiple guesses
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.games.crocodile as _croc
from app.games.crocodile import (
    CrocodileGame,
    broadcast_game_event,
    create_game,
    get_game_history,
    load_game,
    subscribe_game,
    unsubscribe_game,
)
from app.games.judge import GuessJudgement

# ── Shared stubs ──────────────────────────────────────────────────────────────


_EXACT = GuessJudgement(status="hot", score=1.0, hint="Точно!")
_HOT = GuessJudgement(status="hot", score=0.88, hint="Горячо 🔥")
_WARM = GuessJudgement(status="warm", score=0.54, hint="Теплее 🌡️")
_COLD = GuessJudgement(status="cold", score=0.12, hint="Холодно ❄️")
_UNAVAILABLE = None  # returned by _race_generate when LLM is down


def _patch_judge(judgement: GuessJudgement | None, status_str: str | None = None):
    """Patch judge_guess to return a deterministic result."""
    return patch(
        "app.games.judge.judge_guess",
        new=AsyncMock(
            return_value=(
                (status_str or judgement.status, judgement)
                if judgement
                else ("judge_unavailable", GuessJudgement(status="cold", score=0.0, hint=""))
            )
        ),
    )


def _patch_save():
    """Patch CrocodileGame.save to avoid Redis I/O — use in-memory only."""

    async def _mem_save(self):
        _croc._mem_put(self)
        return False  # signals in-memory path

    return patch.object(CrocodileGame, "save", _mem_save)


def _patch_prefetch_noop():
    """Suppress background hint prefetch tasks (they call LLM)."""
    return patch(
        "app.games.crocodile._prefetch_hints",
        new=AsyncMock(return_value=None),
    )


# ── CG-01: Game creation ──────────────────────────────────────────────────────


class TestGameCreation:
    @pytest.mark.asyncio
    async def test_create_game_stores_in_memory(self):
        """create_game must persist to the in-memory store (Redis=None path)."""
        with (
            _patch_save(),
            _patch_prefetch_noop(),
            patch("app.cache.redis_client", None),
        ):
            game = await create_game(
                target_word="крокодил",
                category="Животные",
                lang="ru",
                inline_message_id="msg42",
                creator_id=1001,
            )

        assert game.game_id in _croc._mem_games
        loaded = _croc._mem_get(game.game_id)
        assert loaded is not None
        assert loaded.target_word == "крокодил"
        assert loaded.status == "active"
        assert loaded.creator_id == 1001

    @pytest.mark.asyncio
    async def test_create_game_generates_unique_ids(self):
        """Multiple games must have distinct game_ids."""
        with (
            _patch_save(),
            _patch_prefetch_noop(),
            patch("app.cache.redis_client", None),
        ):
            g1 = await create_game(
                target_word="слон",
                category="Животные",
                lang="ru",
                inline_message_id="msg1",
                creator_id=1001,
            )
            g2 = await create_game(
                target_word="жираф",
                category="Животные",
                lang="ru",
                inline_message_id="msg2",
                creator_id=1002,
            )

        assert g1.game_id != g2.game_id


# ── CG-02: Exact match ────────────────────────────────────────────────────────


class TestExactMatch:
    @pytest.mark.asyncio
    async def test_exact_match_sets_won_status(self):
        """Guessing the exact word must transition game to status='won'."""
        game = CrocodileGame(
            game_id="game-won-test",
            target_word="крокодил",
            category="Животные",
            lang="ru",
            inline_message_id="msg99",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with (
            _patch_judge(_EXACT, status_str="exact_match"),
            _patch_save(),
        ):
            event = await game.process_guess("крокодил")

        assert event["event"] == "result"
        assert event["status"] == "exact_match"
        assert game.status == "won"
        assert event["word"] == "крокодил"
        assert event["score"] == 1.0

    @pytest.mark.asyncio
    async def test_exact_match_skips_llm_for_identical_input(self):
        """DL distance=0 must short-circuit before the LLM is called at all."""
        game = CrocodileGame(
            game_id="game-shortcircuit",
            target_word="мангуст",
            category="Животные",
            lang="ru",
            inline_message_id="msg77",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with (
            patch("app.games.judge._race_generate", new=AsyncMock()) as mock_race,
            patch("app.games.judgement_cache.get_cached_judgement", new=AsyncMock(return_value=None)),
            patch("app.games.judgement_cache.cache_judgement", new=AsyncMock()),
            _patch_save(),
        ):
            event = await game.process_guess("мангуст")

        assert event["status"] == "exact_match"
        mock_race.assert_not_called()


# ── CG-03: Warm / cold guesses ────────────────────────────────────────────────


class TestWarmColdGuesses:
    @pytest.mark.asyncio
    async def test_cold_guess_recorded_in_history(self):
        """A cold guess must appear in the in-memory history."""
        game = CrocodileGame(
            game_id="game-cold",
            target_word="слон",
            category="Животные",
            lang="ru",
            inline_message_id="msg10",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with _patch_judge(_COLD), _patch_save():
            event = await game.process_guess("мышь")

        assert event["status"] == "cold"
        assert event["attempts"] == 1
        history = get_game_history("game-cold")
        assert len(history) == 1
        assert history[0]["word"] == "мышь"
        assert history[0]["status"] == "cold"

    @pytest.mark.asyncio
    async def test_multiple_guesses_increment_attempts(self):
        """Three guesses must yield attempts=3 in the event payload."""
        game = CrocodileGame(
            game_id="game-attempts",
            target_word="носорог",
            category="Животные",
            lang="ru",
            inline_message_id="msg20",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        results = []
        for word in ["кот", "лев", "тигр"]:
            with _patch_judge(_WARM), _patch_save():
                ev = await game.process_guess(word)
            results.append(ev)

        assert results[-1]["attempts"] == 3
        assert len(get_game_history("game-attempts")) == 3


# ── CG-04: Homoglyph / Cyrillic typo tolerance ───────────────────────────────


class TestHomoglyphTolerance:
    @pytest.mark.asyncio
    async def test_single_cyrillic_typo_is_exact_match(self):
        """'монгуст' vs 'мангуст' — DL distance=1 in 7-char word → exact_match."""
        game = CrocodileGame(
            game_id="game-typo",
            target_word="мангуст",
            category="Животные",
            lang="ru",
            inline_message_id="msg30",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with (
            patch("app.games.judge._race_generate", new=AsyncMock()) as mock_race,
            patch("app.games.judgement_cache.get_cached_judgement", new=AsyncMock(return_value=None)),
            patch("app.games.judgement_cache.cache_judgement", new=AsyncMock()),
            _patch_save(),
        ):
            event = await game.process_guess("монгуст")

        assert event["status"] == "exact_match"
        mock_race.assert_not_called()

    @pytest.mark.asyncio
    async def test_two_typos_not_exact_match_goes_to_llm(self):
        """Two-character deviation exceeds DL tolerance → LLM is consulted."""
        game = CrocodileGame(
            game_id="game-twotypos",
            target_word="мангуст",
            category="Животные",
            lang="ru",
            inline_message_id="msg31",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with (
            _patch_judge(_HOT),
            _patch_save(),
        ):
            event = await game.process_guess("монгост")  # 2 changes

        # 2 changes in 7-char word: DL=2, ratio=2/7≈0.28 > threshold
        # Should be NOT exact_match (goes through LLM which we forced to HOT)
        assert event["status"] in ("hot", "warm", "cold")


# ── CG-05: Judge unavailable ──────────────────────────────────────────────────


class TestJudgeUnavailable:
    @pytest.mark.asyncio
    async def test_judge_unavailable_does_not_count_attempt(self):
        """When LLM is unavailable, the attempt count must NOT increase."""
        game = CrocodileGame(
            game_id="game-junavail",
            target_word="тигр",
            category="Животные",
            lang="ru",
            inline_message_id="msg40",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        with (
            patch(
                "app.games.judge.judge_guess",
                new=AsyncMock(return_value=("judge_unavailable", GuessJudgement(status="cold", score=0.0, hint=""))),
            ),
            _patch_save(),
        ):
            event = await game.process_guess("лев")

        assert event["event"] == "judge_unavailable"
        assert len(game.attempts) == 0  # attempt NOT counted


# ── CG-06: Max attempts → lost ────────────────────────────────────────────────


class TestMaxAttempts:
    @pytest.mark.asyncio
    async def test_max_attempts_reached_sets_lost_status(self):
        """On the Nth cold guess (N = max_attempts), game becomes 'lost'."""
        game = CrocodileGame(
            game_id="game-maxattempts",
            target_word="бегемот",
            category="Животные",
            lang="ru",
            inline_message_id="msg50",
            creator_id=1001,
            guesser_id=2001,
            max_attempts=3,  # short game for speed
        )
        _croc._mem_put(game)

        last_event = {}
        for word in ["кот", "лев", "волк"]:
            with _patch_judge(_COLD), _patch_save():
                last_event = await game.process_guess(word)

        assert game.status == "lost"
        assert last_event["event"] == "game_over"
        assert last_event["reason"] == "max_attempts"
        assert last_event["word"] == "бегемот"


# ── CG-07: Surrender ──────────────────────────────────────────────────────────


class TestSurrender:
    @pytest.mark.asyncio
    async def test_surrender_reveals_word_and_sets_lost(self):
        """surrender() must set status=lost and reveal the target word."""
        game = CrocodileGame(
            game_id="game-surrender",
            target_word="верблюд",
            category="Животные",
            lang="ru",
            inline_message_id="msg60",
            creator_id=1001,
            guesser_id=2001,
        )
        _croc._mem_put(game)

        mock_bot = object()  # finalize() patches bot.edit_message_text anyway
        with (
            patch.object(CrocodileGame, "finalize", new=AsyncMock()),
        ):
            event = await game.surrender(mock_bot)

        assert game.status == "lost"
        assert event["event"] == "surrendered"
        assert event["word"] == "верблюд"


# ── CG-08: PubSub fan-out ─────────────────────────────────────────────────────


class TestPubSub:
    @pytest.mark.asyncio
    async def test_broadcast_reaches_all_subscribers(self):
        """broadcast_game_event must deliver to all subscribers."""
        gid = "game-pubsub"
        q1 = subscribe_game(gid)
        q2 = subscribe_game(gid)

        try:
            payload = {"event": "test", "value": 42}
            await broadcast_game_event(gid, payload)

            assert not q1.empty()
            assert not q2.empty()
            assert q1.get_nowait() == payload
            assert q2.get_nowait() == payload
        finally:
            unsubscribe_game(gid, q1)
            unsubscribe_game(gid, q2)

    @pytest.mark.asyncio
    async def test_broadcast_excludes_sender_queue(self):
        """broadcast_game_event with exclude= must skip the excluded queue."""
        gid = "game-pubsub-exclude"
        sender_q = subscribe_game(gid)
        observer_q = subscribe_game(gid)

        try:
            await broadcast_game_event(gid, {"event": "result"}, exclude=sender_q)

            assert sender_q.empty()  # sender skipped
            assert not observer_q.empty()  # observer received
        finally:
            unsubscribe_game(gid, sender_q)
            unsubscribe_game(gid, observer_q)


# ── CG-09: load_game ─────────────────────────────────────────────────────────


class TestLoadGame:
    @pytest.mark.asyncio
    async def test_load_unknown_game_returns_none(self):
        """load_game for a non-existent ID must return None."""
        result = await load_game("does-not-exist-game-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_load_game_returns_correct_instance(self):
        """load_game for a known in-memory game must return the correct game."""
        game = CrocodileGame(
            game_id="game-loadtest",
            target_word="панда",
            category="Животные",
            lang="ru",
            inline_message_id="msgL",
            creator_id=1001,
            guesser_id=None,
        )
        _croc._mem_put(game)

        with patch("app.cache.redis_client", None):
            loaded = await load_game("game-loadtest")

        assert loaded is not None
        assert loaded.target_word == "панда"
        assert loaded.game_id == "game-loadtest"


# ── CG-10: Best score tracking ───────────────────────────────────────────────


class TestBestScoreTracking:
    @pytest.mark.asyncio
    async def test_best_score_updates_on_higher_score(self):
        """best_score must track the maximum score seen across all guesses."""
        game = CrocodileGame(
            game_id="game-bestscore",
            target_word="кенгуру",
            category="Животные",
            lang="ru",
            inline_message_id="msgBS",
            creator_id=1001,
            guesser_id=2001,
            best_score=0.0,
        )
        _croc._mem_put(game)

        with _patch_judge(_COLD), _patch_save():
            await game.process_guess("кот")  # score=0.12

        with _patch_judge(_HOT), _patch_save():
            ev2 = await game.process_guess("прыгун")  # score=0.88

        with _patch_judge(_COLD), _patch_save():
            ev3 = await game.process_guess("мышь")  # score=0.12

        assert round(game.best_score, 2) == 0.88
        assert ev2["best_score_updated"] is True
        assert ev3["best_score_updated"] is False
        assert ev3["best_score"] == pytest.approx(0.88, abs=0.01)

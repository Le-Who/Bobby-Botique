# tests/test_game_judge_integration.py
"""Integration tests for the judge_guess() 4-stage pipeline.

Everything external is mocked:
  - _race_generate  (the LLM race — primary + fallback)
  - cache_judgement / get_cached_judgement  (file LRU)

This lets us test the pipeline logic in isolation without network or disk I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call, patch

import pytest

from app.games.judge import GuessJudgement, judge_guess

# ── Shared stubs ──────────────────────────────────────────────────────────────

_COLD = GuessJudgement(status="cold", score=0.1, hint="Холодно ❄️")
_WARM = GuessJudgement(status="warm", score=0.55, hint="Теплее 🌡️")
_HOT  = GuessJudgement(status="hot",  score=0.85, hint="Горячо 🔥")


# ── judge_guess pipeline ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestJudgeGuessPipeline:

    @pytest.fixture(autouse=True)
    def _patch_cache(self):
        """Default: cache always misses; cache_judgement is a no-op.
        Individual tests can override via nested patches.
        """
        with (
            patch(
                "app.games.judgement_cache.get_cached_judgement",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.games.judgement_cache.cache_judgement",
                new_callable=AsyncMock,
            ),
        ):
            yield

    async def test_exact_match_skips_cache_and_llm(self):
        """DL exact_match must short-circuit before cache or LLM are touched."""
        with patch(
            "app.games.judge._race_generate", new_callable=AsyncMock
        ) as mock_race:
            status, judgement = await judge_guess("крокодил", "крокодил")

        assert status == "exact_match"
        assert judgement.score == 1.0
        mock_race.assert_not_called()

    async def test_typo_exact_match_skips_llm(self):
        """Typo-matched guess (DL within tolerance) must also bypass LLM."""
        with patch(
            "app.games.judge._race_generate", new_callable=AsyncMock
        ) as mock_race:
            # "монгуст" vs "мангуст" — 7 chars, dist=1, allowed=1
            status, judgement = await judge_guess("мангуст", "монгуст")

        assert status == "exact_match"
        mock_race.assert_not_called()

    async def test_cache_hit_returns_cached_result_with_flag(self):
        """Cache hit must return cached judgement with cached=True, no LLM call."""
        cached_j = GuessJudgement(status="warm", score=0.5, hint="Кэш 🗂️", cached=False)

        with (
            patch(
                "app.games.judgement_cache.get_cached_judgement",
                new_callable=AsyncMock,
                return_value=cached_j,
            ),
            patch(
                "app.games.judge._race_generate", new_callable=AsyncMock
            ) as mock_race,
        ):
            status, judgement = await judge_guess("слон", "носорог")

        assert status == "warm"
        assert judgement.cached is True
        mock_race.assert_not_called()

    async def test_llm_result_returned_and_cached(self):
        """On cache miss, LLM result must be returned and fire-and-forget cached.

        judge_guess() does `asyncio.create_task(cache_judgement(...))`.
        `create_task` calls the mock synchronously to obtain the coroutine,
        so mock.assert_called_once() is correct — not assert_awaited_once().
        The patch must target the module where the function lives
        (app.games.judgement_cache) since the import is done locally inside
        judge_guess at call time.
        """
        with (
            patch(
                "app.games.judge._race_generate",
                new_callable=AsyncMock,
                return_value=_HOT,
            ),
            patch(
                "app.games.judgement_cache.cache_judgement",
                new_callable=AsyncMock,
            ) as mock_cache_write,
        ):
            status, judgement = await judge_guess("слон", "мамонт")

        assert status == "hot"
        assert judgement.cached is False
        # create_task(cache_judgement(...)) calls the mock once to get the coroutine
        mock_cache_write.assert_called_once()

    async def test_judge_unavailable_when_all_llm_fail(self):
        """When _race_generate returns None, pipeline must return judge_unavailable.
        The attempt MUST NOT be counted (this is enforced by the caller, but
        we verify the sentinel is returned correctly here).
        """
        with patch(
            "app.games.judge._race_generate",
            new_callable=AsyncMock,
            return_value=None,
        ):
            status, judgement = await judge_guess("слон", "лошадь")

        assert status == "judge_unavailable"
        assert judgement.score == 0.0

    async def test_judge_unavailable_does_not_write_cache(self):
        """judge_unavailable sentinels must NOT be written to the judgement cache."""
        with (
            patch(
                "app.games.judge._race_generate",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.games.judgement_cache.cache_judgement",
                new_callable=AsyncMock,
            ) as mock_cache_write,
        ):
            await judge_guess("слон", "лошадь")

        mock_cache_write.assert_not_called()

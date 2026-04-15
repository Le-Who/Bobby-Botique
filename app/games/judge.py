# /app/games/judge.py
"""Semantic judge for the Crocodile game.

Evaluation pipeline (fastest-first):
  1. Levenshtein exact/typo check  (<1ms, no I/O)
  2. Judgement Cache lookup          (<5ms, Redis)
  3. Race×3 LLM structured output   (500-800ms, Gemini flash-lite)
  4. Timeout/LLM-failure fallback   (<1ms, local heuristic)

The race uses *generate_content* (non-streaming) because the response is
tiny (≤120 tokens) and streaming overhead would add latency, not reduce it.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Models ────────────────────────────────────────────────────────────────────

_PRIMARY_MODEL = "gemini-3.1-flash-lite-preview"
_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Hard timeout for the entire LLM race (all 3 keys must respond within this)
_LLM_TIMEOUT_S = 1.5


# ── Pydantic schema ───────────────────────────────────────────────────────────


class GuessJudgement(BaseModel):
    """Structured output from the semantic judge."""

    status: Literal["cold", "warm", "hot"]
    score: float = Field(ge=0.0, le=1.0)
    hint: str = Field(max_length=80)
    cached: bool = False  # Set by caller; not part of LLM output


# ── System prompt (~50 tokens) ────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Судья игры «Крокодил». Слово: {W}. Догадка: {G}.\n"
    "Оцени семантическую близость (score 0–1). "
    "Статус: cold (<0.3), warm (0.3–0.7), hot (>0.7).\n"
    "Дай смешную подсказку ≤10 слов. Не называй загаданное слово."
)


# ── Levenshtein exact / near-match ────────────────────────────────────────────


def _local_check(target: str, guess: str) -> str | None:
    """Return 'exact_match' if target≈guess, else None.

    Uses difflib.SequenceMatcher (stdlib) — no extra dependency.
    Catches typos with ≥90% similarity (e.g. 'крокадил', 'Кроккодил').
    """
    t = target.lower().strip()
    g = guess.lower().strip()
    if t == g:
        return "exact_match"
    ratio = difflib.SequenceMatcher(None, t, g).ratio()
    if ratio >= 0.90:
        return "exact_match"
    return None


# ── Race×3 non-streaming generate_content ────────────────────────────────────


async def _race_generate(target: str, guess: str) -> GuessJudgement | None:
    """Fire up to 3 Gemini keys simultaneously; return the first valid result.

    Returns None if all attempts fail or timeout.
    """
    from app.agent_use_cases import AgentRequestUseCase
    from app.providers.base import _build_thinking_config, get_provider_for_model
    from app.providers.gemini import get_cached_genai_client

    use_case = AgentRequestUseCase()
    failed_keys: set[str] = set()

    # Resolve up to 3 distinct keys
    keys: list[dict] = []
    resolved_model: str | None = None
    for _ in range(3):
        kd, mdl, _ = await use_case.resolve_ai_request(
            _PRIMARY_MODEL,
            excluded_key_hashes=failed_keys | {k["key_hash"] for k in keys},
        )
        if kd and mdl:
            keys.append(kd)
            resolved_model = mdl
        else:
            break  # No more keys

    if not keys or not resolved_model:
        logger.warning("Judge: no API keys available")
        return None

    prompt = _SYSTEM_PROMPT.format(W=target, G=guess)
    tc = _build_thinking_config(resolved_model, "low")

    from google.genai import types as _gtypes

    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=GuessJudgement.model_json_schema(),
        temperature=0.3,
        max_output_tokens=120,
    )
    if tc:
        config.thinking_config = tc

    async def _one_call(api_key: str, model: str) -> GuessJudgement | None:
        try:
            client = get_cached_genai_client(api_key)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                ),
                timeout=_LLM_TIMEOUT_S,
            )
            text = getattr(response, "text", None) or ""
            if not text:
                return None
            data = json.loads(text)
            return GuessJudgement.model_validate(data)
        except Exception as exc:
            logger.debug("Judge race call failed (%s): %s", model, exc)
            return None

    # Launch all 3 concurrently; return first non-None
    coros = [_one_call(kd["api_key"], resolved_model) for kd in keys]
    tasks = [asyncio.create_task(c) for c in coros]

    result: GuessJudgement | None = None
    pending = set(tasks)
    try:
        while pending and result is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                r = task.result()
                if r is not None:
                    result = r
                    break
    finally:
        for t in pending:
            t.cancel()

    return result


# ── Fallback heuristic (no LLM, no external I/O) ─────────────────────────────


def _fallback_judgement(target: str, guess: str) -> GuessJudgement:
    """Compute a naive score from string similarity when LLM is unavailable."""
    ratio = difflib.SequenceMatcher(None, target.lower(), guess.lower()).ratio()
    if ratio >= 0.7:
        status, hint = "hot", "Очень близко! Попробуй ещё раз 🔥"
    elif ratio >= 0.4:
        status, hint = "warm", "Что-то похожее… Ещё разок 😐"
    else:
        status, hint = "cold", "Не то! Подумай с другой стороны ❄️"
    return GuessJudgement(status=status, score=round(ratio, 2), hint=hint)  # type: ignore[arg-type]


# ── Public entry point ────────────────────────────────────────────────────────


async def judge_guess(target: str, guess: str) -> tuple[str, GuessJudgement]:
    """Evaluate a guess against the target word.

    Returns:
        (status_str, judgement) where status_str may be "exact_match",
        "cold", "warm", or "hot".

    Pipeline:
        Levenshtein → Cache → Race×3 LLM → fallback
    """
    from app.games.judgement_cache import cache_judgement, get_cached_judgement

    # 1. Local exact / near-match (< 1ms, no I/O)
    local = _local_check(target, guess)
    if local:
        j = GuessJudgement(status="hot", score=1.0, hint="Угадано! 🎉")
        return "exact_match", j

    # 2. Judgement cache (< 5ms, Redis)
    cached = await get_cached_judgement(target, guess)
    if cached is not None:
        cached.cached = True
        return cached.status, cached

    # 3. Race×3 LLM
    result = await _race_generate(target, guess)

    # 4. Fallback if LLM unavailable / timed out
    if result is None:
        logger.info("Judge: LLM unavailable — using fallback heuristic")
        result = _fallback_judgement(target, guess)

    # Store in cache (fire-and-forget, non-blocking)
    asyncio.create_task(cache_judgement(target, guess, result))  # noqa: RUF006

    return result.status, result

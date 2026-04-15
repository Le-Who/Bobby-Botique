# /app/games/judge.py
"""Semantic judge for the Crocodile game.

Evaluation pipeline (fastest-first):
  1. Damerau-Levenshtein typo check  (<1ms, no I/O)
     Length-dependent tolerance: ≤4 chars → 0 edits, 5-7 → 1 edit, ≥8 → 2 edits.
  2. Judgement Cache lookup          (<5ms, local file LRU)
  3. Race×3 LLM structured output   (300-2000ms, Gemini flash-lite)
  4. judge_unavailable sentinel      (<1ms, no attempt counted by caller)

The race uses *generate_content* (non-streaming) because the response is
tiny (≤120 tokens) and streaming overhead would add latency, not reduce it.

IMPORTANT: There is intentionally NO string-similarity fallback for warm/cold
scoring. Using character-level Levenshtein to measure *semantic* distance is
mathematically wrong ('парашют' ≈ 'порошок' by letters, but worlds apart in
meaning). If the LLM race fails we return 'judge_unavailable' so the caller
can decline to count the attempt rather than lie to the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Models ────────────────────────────────────────────────────────────────────

_PRIMARY_MODEL = "gemini-3.1-flash-lite-preview"
_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Timeout for entire LLM race. Increased from 1.5s → 3.0s:
# Gemini Flash-Lite typically responds in 300-800ms; 1.5s occasionally fails
# on SSL-handshake throttling. 3.0s covers >99.9% of realistic cases.
_LLM_TIMEOUT_S = 3.0


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class GuessJudgement(BaseModel):
    """Structured output from the semantic judge."""

    status: Literal["cold", "warm", "hot"]
    score: float = Field(ge=0.0, le=1.0)
    hint: str = Field(max_length=80)
    cached: bool = False  # Set by caller; not part of LLM output


class HintsOutput(BaseModel):
    """Structured output for progressive game hints (exactly 3 items)."""

    hints: list[str] = Field(min_length=3, max_length=3)


# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Судья игры «Крокодил». Загаданное слово: «{W}». Догадка: «{G}».\n"
    "ВАЖНО: оценивай ТОЛЬКО смысловую близость — буквенное сходство ИГНОРИРУЙ.\n"
    "Плохой пример: «парашют» и «порошок» — семантически далеки, хотя буквы похожи.\n"
    "score 0–1 → статус: cold<0.3, warm 0.3–0.7, hot>0.7.\n"
    "Если догадка — это загаданное слово в другой форме/падеже/числе/уменьш. — score≥0.92.\n"
    "Дай короткую смешную подсказку ≤10 слов. Не называй загаданное слово прямо."
)

_HINTS_PROMPT = (
    "Игра «Крокодил».\n"
    "Загаданное слово: «{W}» (категория: {C}).\n"
    "Дай РОВНО 3 подсказки на русском языке в JSON {{\"hints\": [\"...\",\"...\",\".\"]}}.\n"
    "Подсказка 1 (неочевидная): намёк лишь на широкую категорию/область. ≤12 слов.\n"
    "Подсказка 2 (средняя): ключевое свойство или метафора. ≤12 слов.\n"
    "Подсказка 3 (почти прямая): описание без однокоренных слов и без самого слова. ≤12 слов.\n"
    "Не называй слово прямо ни в одной подсказке."
)


# ── Damerau-Levenshtein typo check ───────────────────────────────────────────


def _damerau_levenshtein(s: str, t: str) -> int:
    """Restricted Damerau-Levenshtein distance (optimal string alignment).

    Counts insertions, deletions, substitutions and adjacent transpositions.
    O(m*n) — adequate for words up to ~60 chars.
    """
    m, n = len(s), len(t)
    d = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        d[i][0] = i
    for j in range(n + 1):
        d[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if s[i - 1] == t[j - 1] else 1
            d[i][j] = min(
                d[i - 1][j] + 1,           # deletion
                d[i][j - 1] + 1,           # insertion
                d[i - 1][j - 1] + cost,    # substitution
            )
            # Adjacent transposition (Damerau extension)
            if i > 1 and j > 1 and s[i - 1] == t[j - 2] and s[i - 2] == t[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + cost)
    return d[m][n]


def _allowed_edits(length: int) -> int:
    """Max edit distance to still consider a guess a correct typo.

    ≤4 chars  → 0 edits  (Кот ≠ Кит — distinct short words)
    5–7 chars → 1 edit   (Мангуст → Монгуст ✓)
    ≥8 chars  → 2 edits  (Крокодил → Крокодил ✓, longer words allow more)
    """
    if length <= 4:
        return 0
    if length <= 7:
        return 1
    return 2


def _local_check(target: str, guess: str) -> str | None:
    """Return 'exact_match' if guess is target with at most N typos, else None.

    Uses Damerau-Levenshtein with a length-dependent tolerance window.
    'Монгуст' matches 'Мангуст' (7 chars, 1 edit allowed).
    'Кит' does NOT match 'Кот' (3 chars, 0 edits allowed).
    """
    t = target.lower().strip()
    g = guess.lower().strip()
    if t == g:
        return "exact_match"
    dist = _damerau_levenshtein(t, g)
    if dist <= _allowed_edits(len(t)):
        return "exact_match"
    return None


# ── Race×3 non-streaming generate_content ────────────────────────────────────


async def _race_generate(target: str, guess: str) -> GuessJudgement | None:
    """Fire up to 3 Gemini keys simultaneously; return the first valid result.

    Returns None if all attempts fail or timeout.

    NOTE: thinking_config is intentionally NOT set here.
    Structured JSON output (response_schema) and thinking_config are mutually
    exclusive in the Gemini API — combining them produces empty responses.
    """
    from app.agent_use_cases import AgentRequestUseCase
    from app.providers.gemini import get_cached_genai_client

    use_case = AgentRequestUseCase()
    failed_keys: set[str] = set()

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
            break

    if not keys or not resolved_model:
        logger.warning("Judge: no API keys available")
        return None

    prompt = _SYSTEM_PROMPT.format(W=target, G=guess)

    from google.genai import types as _gtypes

    # Do NOT add thinking_config: structured output (response_schema) and
    # thinking are mutually exclusive — Gemini returns empty text when both set.
    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=GuessJudgement.model_json_schema(),
        temperature=0.3,
        max_output_tokens=120,
    )

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
            logger.warning("Judge race call failed (model=%s): %s", model, exc)
            return None

    # Launch all 3 concurrently; return first non-None.
    # Extract api_key strings before spawning so raw key material doesn't
    # linger in frame locals if an exception propagates with exc_info=True.
    api_keys_for_race = [kd["api_key"] for kd in keys]
    keys.clear()
    coros = [_one_call(ak, resolved_model) for ak in api_keys_for_race]
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


# ── Progressive hint generation ───────────────────────────────────────────────


async def generate_hints(word: str, category: str) -> list[str]:
    """Generate 3 progressive hints for the given word asynchronously.

    Returns an empty list on any failure; callers must handle gracefully.
    Called from a background asyncio.Task so latency does not block the user.

    NOTE: thinking_config not used — incompatible with response_schema.
    """
    from app.agent_use_cases import AgentRequestUseCase
    from app.providers.gemini import get_cached_genai_client

    use_case = AgentRequestUseCase()
    kd, mdl, _ = await use_case.resolve_ai_request(_PRIMARY_MODEL)
    if not kd or not mdl:
        logger.warning("generate_hints: no API key for word=%r", word)
        return []

    prompt = _HINTS_PROMPT.format(W=word, C=category)

    from google.genai import types as _gtypes

    # Do NOT add thinking_config: structured output (response_schema) and
    # thinking are mutually exclusive — Gemini returns empty text when both set.
    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=HintsOutput.model_json_schema(),
        temperature=0.7,
        max_output_tokens=250,
    )

    try:
        api_key = kd["api_key"]
        client = get_cached_genai_client(api_key)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=mdl,
                contents=prompt,
                config=config,
            ),
            timeout=12.0,  # Background task — can wait longer than guess judge
        )
        text = getattr(response, "text", None) or ""
        if not text:
            return []
        data = json.loads(text)
        validated = HintsOutput.model_validate(data)
        logger.debug("Hints generated for word=%r: %s", word, validated.hints)
        return validated.hints
    except Exception as exc:
        logger.warning("generate_hints failed word=%r: %s", word, exc)
        return []


# ── Public entry point ────────────────────────────────────────────────────────


async def judge_guess(target: str, guess: str) -> tuple[str, GuessJudgement]:
    """Evaluate a guess against the target word.

    Returns:
        (status_str, judgement) where status_str is one of:
          'exact_match'       — correct answer (including typo tolerance)
          'cold'/'warm'/'hot' — LLM semantic result
          'judge_unavailable' — all LLM keys timed out or failed.
                                Caller MUST NOT count this as an attempt.

    Pipeline:
        Damerau-Levenshtein → Cache → Race×3 LLM → judge_unavailable
    """
    from app.games.judgement_cache import cache_judgement, get_cached_judgement

    # 1. Local exact / near-match (<1ms, no I/O)
    local = _local_check(target, guess)
    if local:
        j = GuessJudgement(status="hot", score=1.0, hint="Угадано! 🎉")
        return "exact_match", j

    # 2. Judgement cache (<5ms, local file)
    cached = await get_cached_judgement(target, guess)
    if cached is not None:
        cached.cached = True
        return cached.status, cached

    # 3. Race×3 LLM
    result = await _race_generate(target, guess)

    # 4. LLM unavailable — deliberately NO character-similarity fallback.
    #    String metrics (Levenshtein, SequenceMatcher) measure orthographic
    #    distance, not semantic distance. Using them to say "warm" or "cold"
    #    is factually wrong and unfair to the player.
    #    Instead: return a sentinel so the caller skips counting the attempt.
    if result is None:
        logger.info("Judge: LLM unavailable — returning judge_unavailable sentinel")
        sentinel = GuessJudgement(
            status="cold",
            score=0.0,
            hint="🤔 Крокодил задумался... Попытка не засчитана!",
        )
        return "judge_unavailable", sentinel

    # Cache result for future identical guesses (fire-and-forget)
    asyncio.create_task(cache_judgement(target, guess, result))  # noqa: RUF006

    return result.status, result

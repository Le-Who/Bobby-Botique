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
import time
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Models ────────────────────────────────────────────────────────────────────

_PRIMARY_MODEL = "gemini-3.1-flash-lite-preview"
_FALLBACK_MODEL = "gemini-2.5-flash-lite"

# Primary race: concurrent across up to 3 keys + optional Vertex AI — wall time = timeout of
# the fastest winner, so 7s is safe even under degraded conditions.
_LLM_TIMEOUT_S = 7.0
# Fallback: single key, last resort — give it twice as long; player waits, not retypes.
_LLM_FALLBACK_TIMEOUT_S = 14.0

# ── Circuit breaker for _PRIMARY_MODEL ────────────────────────────────────────
# When _PRIMARY_MODEL is experiencing a model-level 503 outage (all keys return
# UNAVAILABLE), every judge call burns 3 keys × 3s timeout before falling back.
# The circuit short-circuits that: after any all-fail race, we skip the primary
# for _PRIMARY_CIRCUIT_COOLDOWN_S and go straight to the fallback model.
# After the cooldown one probe attempt is made; the circuit closes on success.
#
# asyncio is single-threaded — float reassignment is safe without a Lock.
_primary_circuit_open_until: float = 0.0   # monotonic timestamp; 0.0 = closed
_PRIMARY_CIRCUIT_COOLDOWN_S: float = 120.0  # 2 min between probes


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class GuessJudgement(BaseModel):
    """Structured output from the semantic judge."""

    status: Literal["cold", "warm", "hot"]
    score: float = Field(ge=0.0, le=1.0)
    hint: str = Field(max_length=255)
    cached: bool = False  # Set by caller; not part of LLM output


class HintsOutput(BaseModel):
    """Structured output for progressive game hints (exactly 3 items)."""

    hints: list[str] = Field(min_length=3, max_length=3)


# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — остроумный и непредсказуемый судья игры «Крокодил».\n"
    "Загаданное слово: «{W}». Догадка игрока: «{G}».\n"
    "\n"
    "ОЦЕНКА: score 0.0–1.0 — ТОЛЬКО смысловая близость (cold<0.3, warm 0.3–0.7, hot>0.7).\n"
    "Если dogadka — прямой синоним или другая форма «{W}» — score≥0.92.\n"
    "\n"
    "ПОДСКАЗКА (поле hint) — комментируй «{G}» как судья, держи интригу:\n"
    "• cold (score<0.3): игрок явно промахнулся. Будь иронично-удивлённым или лаконичным. Не повторяй шаблоны.\n"
    "• warm (0.3–0.7): похвали направление мысли, намекни что он на верном пути.\n"
    "• hot (>0.7): восторженно: он почти угадал! Замотивируй подумать ещё чуть-чуть.\n"
    "\n"
    "СТРОГИЕ ЗАПРЕТЫ:\n"
    "— НЕ называй свойства, форму, цвет, назначение слова «{W}» (не раскрывай ответ).\n"
    "— НЕ используй однокоренные слова к «{W}».\n"
    "— НЕ повторяй фразы между разными ответами — каждый раз придумывай новую формулировку.\n"
    "— НЕ используй клише: 'совсем другая опера', 'мимо', 'другая история', 'не то'.\n"
    "Максимум 12 слов в подсказке."
)

_HINTS_PROMPT = (
    "Игра «Крокодил».\n"
    "Загаданное слово: «{W}»{C_STR}.\n"
    "\n"
    "Дай РОВНО 3 подсказки на русском языке в JSON {{\"hints\": [\"...\",\"...\",\"...\"]}}.\n"
    "Подсказка 1 (неочевидная): намёк лишь на широкую область/тип. ≤12 слов.\n"
    "Подсказка 2 (средняя): ключевое свойство, метафора или ассоциация. ≤12 слов.\n"
    "Подсказка 3 (почти прямая): детальное описание без однокоренных слов и без самого слова. ≤12 слов.\n"
    "\n"
    "ОБЯЗАТЕЛЬНАЯ ПРОВЕРКА: перед тем как вернуть JSON, мысленно проверь — "
    "каждая из 3 подсказок однозначно указывает именно на «{W}», а НЕ на похожее слово "
    "(например, если слово «Германия», подсказки не должны подходить к «Италии»).\n"
    "Если подсказка неточна — перепиши её. Не называй слово прямо."
)


# ── Damerau-Levenshtein typo check ───────────────────────────────────────────


_EN_TO_RU_HOMOGLYPHS = {
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 
    'x': 'х', 'y': 'у', 'k': 'к', 'm': 'м', 't': 'т', 
    'h': 'н', 'b': 'в'
}
_RU_TO_EN_HOMOGLYPHS = {v: k for k, v in _EN_TO_RU_HOMOGLYPHS.items()}


def _homogenize_pair(target: str, guess: str) -> tuple[str, str]:
    """Normalize alphabets to prevent Cyrillic/Latin homoglyph mismatch.
    (e.g., 'a' typed in English matches 'а' typed in Russian).
    """
    t, g = target.lower().strip(), guess.lower().strip()
    
    t_ru = sum(1 for c in t if '\u0400' <= c <= '\u04ff')
    g_ru = sum(1 for c in g if '\u0400' <= c <= '\u04ff')
    
    if t_ru > 0 or g_ru > 0:
        # Treat as Cyrillic
        t_clean = "".join(_EN_TO_RU_HOMOGLYPHS.get(c, c) for c in t)
        g_clean = "".join(_EN_TO_RU_HOMOGLYPHS.get(c, c) for c in g)
    else:
        # Treat as Latin
        t_clean = "".join(_RU_TO_EN_HOMOGLYPHS.get(c, c) for c in t)
        g_clean = "".join(_RU_TO_EN_HOMOGLYPHS.get(c, c) for c in g)
        
    return t_clean, g_clean


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
    t, g = _homogenize_pair(target, guess)
    
    if t == g:
        return "exact_match"
    dist = _damerau_levenshtein(t, g)
    if dist <= _allowed_edits(len(t)):
        return "exact_match"
    return None


# ── Key suspension helper ────────────────────────────────────────────────────


async def _suspend_key_safe(
    key_hash: str, model: str, category: str, error_text: str
) -> None:
    """Suspend a key in the background; swallows exceptions so the caller never crashes.

    Called as a fire-and-forget asyncio.Task from _one_call so it doesn't
    add latency to the race itself.
    """
    try:
        from app.repos.keys import get_key_status_manager
        await get_key_status_manager().suspend_key(key_hash, model, category, error_text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Judge: key suspension task failed for %s: %s", key_hash[:8], exc)


# ── Race×3 non-streaming generate_content ────────────────────────────────────


async def _race_generate(target: str, guess: str) -> GuessJudgement | None:
    """Fire up to 3 Gemini keys simultaneously; return the first valid result.

    Fallback chain:
        1. Race up to 3 keys on _PRIMARY_MODEL (gemini-3.1-flash-lite-preview).
        2. If all fail (503 / timeout / 429), retry once with _FALLBACK_MODEL.
        3. If still None → caller receives judge_unavailable sentinel.

    Key rotation on 429 / quota exhaustion:
        Each _one_call classifies the exception via classify_key_error() and
        fires _suspend_key_safe() as a background task, writing the key into
        key_model_status (suspended until midnight PT for quota errors).
        resolve_ai_request() already filters suspended keys by key_hash, so
        the next round will automatically pick a different key.

    NOTE: thinking_config is intentionally NOT set here.
    Structured JSON output (response_schema) and thinking_config are mutually
    exclusive in the Gemini API — combining them produces empty responses.
    """
    from google.genai import types as _gtypes

    from app.agent_use_cases import AgentRequestUseCase
    from app.errors import classify_key_error
    from app.metrics import metrics_collector
    from app.providers.gemini import get_cached_genai_client
    from app.repos.keys import get_key_status_manager

    use_case = AgentRequestUseCase()
    status_mgr = get_key_status_manager()
    prompt = _SYSTEM_PROMPT.format(W=target, G=guess)

    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=GuessJudgement.model_json_schema(),
        temperature=0.5,
        max_output_tokens=200,
    )

    async def _one_call(api_key: str, key_hash: str, model: str, timeout: float = _LLM_TIMEOUT_S) -> GuessJudgement | None:
        """Single key attempt. On any error: classify → suspend key → return None."""
        try:
            client = get_cached_genai_client(api_key)
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                ),
                timeout=timeout,
            )
            text = getattr(response, "text", None) or ""
            if not text:
                return None
            data = json.loads(text)
            result = GuessJudgement.model_validate(data)
            asyncio.create_task(  # noqa: RUF006
                status_mgr.record_success(key_hash, model)
            )
            # Increment usage counter on success (fire-and-forget)
            asyncio.create_task(  # noqa: RUF006
                use_case.increment_key_usage(key_hash, model, use_openrouter=False)
            )
            return result
        except Exception as exc:
            err_text = str(exc)
            logger.warning("Judge race call failed (model=%s): %s", model, exc)
            # Classify error and suspend key — prevents the same exhausted key
            # from being re-used in subsequent resolve_ai_request() calls.
            category = classify_key_error(err_text)
            asyncio.create_task(  # noqa: RUF006
                _suspend_key_safe(key_hash, model, category, err_text[:500])
            )
            return None
        finally:
            # Record every LLM call attempt in metrics (success OR failure)
            asyncio.create_task(  # noqa: RUF006
                metrics_collector.record_api_call("gemini_judge", model=model)
            )

    async def _run_race(
        key_pairs: list[tuple[str, str]], model: str, timeout: float = _LLM_TIMEOUT_S
    ) -> GuessJudgement | None:
        """Launch all (api_key, key_hash) pairs concurrently; return first non-None result."""
        tasks = [
            asyncio.create_task(_one_call(ak, kh, model, timeout))
            for ak, kh in key_pairs
        ]
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

    # ── Circuit-breaker check ─────────────────────────────────────────────────
    # When _PRIMARY_MODEL is globally 503-ing, the circuit is open and we skip
    # straight to fallback to save ~9s of guaranteed-fail latency per guess.
    global _primary_circuit_open_until
    _now = time.monotonic()
    _circuit_open = _now < _primary_circuit_open_until
    if _circuit_open:
        logger.debug(
            "Judge: primary model circuit open (%.0fs remaining) — skipping to fallback",
            _primary_circuit_open_until - _now,
        )

    # ── Primary model race (Gemini API keys + optional Vertex AI) ───────────────
    if not _circuit_open:
        from app.providers.gemini import get_vertex_client

        async def _one_vertex_call() -> GuessJudgement | None:
            """Race _PRIMARY_MODEL on Vertex AI Express — same SDK, different endpoint.

            Vertex AI infrastructure is empirically more stable under high-demand
            503 storms than the Gemini API endpoint.  Returns None on any error so
            the race degrades gracefully when Vertex AI is not configured or fails.
            """
            vertex_client = get_vertex_client()
            if vertex_client is None:
                return None
            try:
                resp = await asyncio.wait_for(
                    vertex_client.aio.models.generate_content(
                        model=_PRIMARY_MODEL,
                        contents=prompt,
                        config=config,
                    ),
                    timeout=_LLM_TIMEOUT_S,
                )
                resp_text = getattr(resp, "text", None) or ""
                if not resp_text:
                    return None
                return GuessJudgement.model_validate(json.loads(resp_text))
            except Exception as exc:
                logger.debug("Judge: Vertex AI attempt failed: %s", exc)
                return None

        # Resolve up to 3 Gemini API keys
        keys: list[dict] = []
        resolved_model: str | None = None
        for _ in range(3):
            kd, mdl, _ = await use_case.resolve_ai_request(
                _PRIMARY_MODEL,
                excluded_key_hashes={k["key_hash"] for k in keys},
            )
            if kd and mdl:
                keys.append(kd)
                resolved_model = mdl
            else:
                break

        resolved_model = resolved_model or _PRIMARY_MODEL
        primary_pairs = [(kd["api_key"], kd["key_hash"]) for kd in keys]
        keys.clear()

        # Concurrent task list: Gemini API key pool + Vertex AI.
        # _one_vertex_call() returns None instantly when Vertex AI is not configured.
        all_tasks: list[asyncio.Task] = [  # type: ignore[type-arg]
            asyncio.create_task(_one_call(ak, kh, resolved_model))
            for ak, kh in primary_pairs
        ]
        all_tasks.append(asyncio.create_task(_one_vertex_call()))

        result: GuessJudgement | None = None
        pending = set(all_tasks)
        try:
            while pending and result is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    r = finished.result()
                    if r is not None:
                        result = r
                        break
        finally:
            for t in pending:
                t.cancel()

        if result is not None:
            if _primary_circuit_open_until > 0.0:
                logger.info("Judge: primary race recovered — circuit closed")
                _primary_circuit_open_until = 0.0
            return result

        # All Gemini keys + Vertex AI failed — open circuit
        _primary_circuit_open_until = time.monotonic() + _PRIMARY_CIRCUIT_COOLDOWN_S
        logger.warning(
            "Judge: primary model %r all-fail — circuit opened for %.0fs, routing to %r",
            resolved_model,
            _PRIMARY_CIRCUIT_COOLDOWN_S,
            _FALLBACK_MODEL,
        )

    # ── Fallback model race ───────────────────────────────────────────────────
    # Primary model race returned nothing (503 / 429 / all keys failed).
    # resolve_ai_request will skip keys already suspended by _one_call above.
    fallback_kd, fallback_mdl, _ = await use_case.resolve_ai_request(_FALLBACK_MODEL)
    if fallback_kd and fallback_mdl:
        fallback_pair = [(fallback_kd["api_key"], fallback_kd["key_hash"])]
        result = await _run_race(fallback_pair, fallback_mdl, timeout=_LLM_FALLBACK_TIMEOUT_S)
        if result is not None:
            logger.info("Judge: fallback model %r succeeded", fallback_mdl)
            return result
        logger.warning("Judge: fallback model %r also failed", fallback_mdl)
    else:
        logger.warning("Judge: no keys available for fallback model %r", _FALLBACK_MODEL)

    return None


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

    c_str = f" (категория: {category})" if category and "особое" not in category.lower() else ""
    prompt = _HINTS_PROMPT.format(W=word, C_STR=c_str)

    from google.genai import types as _gtypes

    # Do NOT add thinking_config: structured output (response_schema) and
    # thinking are mutually exclusive — Gemini returns empty text when both set.
    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=HintsOutput.model_json_schema(),
        temperature=0.3,   # Factual task — low temp prevents hallucination of wrong word
        max_output_tokens=300,  # Extra tokens to allow self-check reasoning before JSON
    )

    async def _try_generate(model: str) -> list[str] | None:
        kd, mdl, _ = await use_case.resolve_ai_request(model)
        if not kd or not mdl:
            return None
        try:
            client = get_cached_genai_client(kd["api_key"])
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=mdl,
                    contents=prompt,
                    config=config,
                ),
                timeout=25.0,  # Background task — increased timeout (hints only show UI after 10s)
            )
            text = getattr(response, "text", None) or ""
            if not text:
                return None
            data = json.loads(text)
            validated = HintsOutput.model_validate(data)
            logger.debug("Hints generated for word=%r via %s: %s", word, mdl, validated.hints)
            return validated.hints
        except Exception as exc:
            logger.warning("generate_hints failed (model=%s) for word=%r: %s", mdl, word, exc)
            return None

    hints = await _try_generate(_PRIMARY_MODEL)
    if hints is not None:
        return hints

    logger.warning("generate_hints: primary model failed, trying fallback %r", _FALLBACK_MODEL)
    hints = await _try_generate(_FALLBACK_MODEL)
    if hints is not None:
        return hints

    logger.warning("generate_hints: all models failed for word=%r", word)
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
    from app.metrics import metrics_collector

    t0 = time.monotonic()

    # 1. Local exact / near-match (<1ms, no I/O)
    local = _local_check(target, guess)
    if local:
        j = GuessJudgement(status="hot", score=1.0, hint="Угадано! 🎉")
        asyncio.create_task(  # noqa: RUF006
            metrics_collector.record_request("judge", time.monotonic() - t0, success=True)
        )
        return "exact_match", j

    # 2. Judgement cache (<5ms, local file)
    cached = await get_cached_judgement(target, guess)
    if cached is not None:
        cached.cached = True
        
        # Retroactive fix for already cached hot synonyms:
        if cached.score >= 0.92:
            asyncio.create_task(metrics_collector.record_request("judge", time.monotonic() - t0, success=True)) # noqa: RUF006
            return "exact_match", cached
            
        asyncio.create_task(  # noqa: RUF006
            metrics_collector.record_request("judge", time.monotonic() - t0, success=True)
        )
        return cached.status, cached

    # 3. Race×3 LLM
    result = await _race_generate(target, guess)

    elapsed = time.monotonic() - t0

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
        asyncio.create_task(  # noqa: RUF006
            metrics_collector.record_request("judge", elapsed, success=False)
        )
        return "judge_unavailable", sentinel

    status_str = result.status
    if result.score >= 0.92:
        status_str = "exact_match"
        result.status = "hot"
        result.score = 1.0
        result.hint = "Угадано! 🎉"

    # Cache result for future identical guesses (fire-and-forget)
    asyncio.create_task(cache_judgement(target, guess, result))  # noqa: RUF006

    asyncio.create_task(  # noqa: RUF006
        metrics_collector.record_request("judge", elapsed, success=True)
    )
    return status_str, result

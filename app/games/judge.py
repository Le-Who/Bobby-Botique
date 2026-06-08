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
import functools
import logging
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.games.ai_budget import (
    HintGenerationMode,
    acquire_background_slot,
    acquire_foreground_slot,
    record_result,
)
from app.utils.background_tasks import submit_task
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

# ── Models ────────────────────────────────────────────────────────────────────

_PRIMARY_MODEL = "gemini-3.1-flash-lite"
_FALLBACK_MODEL = "gemini-2.5-flash-lite"
_HINTS_AI_STUDIO_MODEL = "gemini-3.5-flash"
_HINTS_VERTEX_MODEL = "gemini-3.1-flash-lite"
_HINTS_OPENCODE_MODEL_CANDIDATES = (
    "opencode-go/glm-5.1",
    "opencode-go/qwen3.6-plus",
    "opencode-go/glm-5",
    "opencode-go/kimi-k2.5",
    "opencode-go/qwen3.5-plus",
)

# Primary race: concurrent across up to 3 keys + optional Vertex AI — wall time = timeout of
# the fastest winner, so 7s is safe even under degraded conditions.
_LLM_TIMEOUT_S = 7.0
# Fallback: single key, last resort — give it twice as long; player waits, not retypes.
_LLM_FALLBACK_TIMEOUT_S = 14.0
_HINTS_TIMEOUT_S = 18.0

# ── Circuit breaker for _PRIMARY_MODEL ────────────────────────────────────────
# When _PRIMARY_MODEL is experiencing a model-level 503 outage (all keys return
# UNAVAILABLE), every judge call burns 3 keys × 3s timeout before falling back.
# The circuit short-circuits that: after any all-fail race, we skip the primary
# for _PRIMARY_CIRCUIT_COOLDOWN_S and go straight to the fallback model.
# After the cooldown one probe attempt is made; the circuit closes on success.
#
# asyncio is single-threaded — float reassignment is safe without a Lock.
_primary_circuit_open_until: float = 0.0  # monotonic timestamp; 0.0 = closed
_PRIMARY_CIRCUIT_COOLDOWN_S: float = 120.0  # 2 min between probes


def _budget_provider_for_model(model_name: str, lane_type: str | None = None) -> str:
    from app.providers.base import is_freetheai_model

    if lane_type == "vertex":
        return "vertex_express"
    if model_name.startswith("opencode-go/"):
        return "opencode_go"
    if is_freetheai_model(model_name):
        return "freetheai"
    if "/" in model_name:
        return "openrouter"
    return "ai_studio"


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class GuessJudgement(BaseModel):
    """Structured output from the semantic judge."""

    status: Literal["cold", "warm", "hot"]
    score: float = Field(ge=0.0, le=1.0)
    hint: str = Field(max_length=255)
    interpreted_domain: str | None = None
    ambiguity_flag: bool = False
    cached: bool = False  # Set by caller; not part of LLM output


class HintsOutput(BaseModel):
    """Structured output for progressive game hints (exactly 3 items)."""

    hints: list[str] = Field(min_length=3, max_length=3)


# ── System prompts ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — остроумный и непредсказуемый судья игры «Крокодил».\n"
    "Загаданное слово: «{W}». Догадка игрока: «{G}».\n"
    "Тема игры: «{C}». topic_id: «{T}».\n"
    "Смысловой контекст: «{S}».\n"
    "\n"
    "ОЦЕНКА: score 0.0–1.0 — ТОЛЬКО смысловая близость (cold<0.3, warm 0.3–0.7, hot>0.7).\n"
    "Если dogadka — прямой синоним или другая форма «{W}» — score≥0.92.\n"
    "ОБЯЗАТЕЛЬНО: оценивай слово только в рамках темы «{C}». Игнорируй другие значения и омонимы.\n"
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
    'Дай РОВНО 3 подсказки на русском языке в JSON {{"hints": ["...","...","..."]}}.\n'
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
    "a": "а",
    "c": "с",
    "e": "е",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
    "k": "к",
    "m": "м",
    "t": "т",
    "h": "н",
    "b": "в",
}
_RU_TO_EN_HOMOGLYPHS = {v: k for k, v in _EN_TO_RU_HOMOGLYPHS.items()}


_YO_MAP = str.maketrans("ёЁ", "еЕ")
_STRIP_PUNCT = str.maketrans("", "", ".,!?;:…—–-")


def _homogenize_pair(target: str, guess: str) -> tuple[str, str]:
    """Normalize alphabets + Ё/Е + trailing punctuation before any local comparison.

    Handles:
    * Cyrillic/Latin homoglyphs (e.g. 'a' EN matches 'а' RU).
    * «Ё» vs «Е» (ёжик == ежик for game purposes).
    * Trailing punctuation a user might accidentally type ("кот." → "кот").
    """
    # 1. Lowercase + strip outer whitespace
    t, g = target.lower().strip(), guess.lower().strip()

    # 2. Ё → Е normalisation (Cyrillic-agnostic, applies to both)
    t = t.translate(_YO_MAP)
    g = g.translate(_YO_MAP)

    # 3. Strip common punctuation characters that sneak into guesses
    t = t.translate(_STRIP_PUNCT).strip()
    g = g.translate(_STRIP_PUNCT).strip()

    # 4. Homoglyph normalization (Cyrillic/Latin)
    t_ru = sum(1 for c in t if "\u0400" <= c <= "\u04ff")
    g_ru = sum(1 for c in g if "\u0400" <= c <= "\u04ff")

    if t_ru > 0 or g_ru > 0:
        # Treat as Cyrillic: convert any Latin homoglyphs to Cyrillic
        t_clean = "".join(_EN_TO_RU_HOMOGLYPHS.get(c, c) for c in t)
        g_clean = "".join(_EN_TO_RU_HOMOGLYPHS.get(c, c) for c in g)
    else:
        # Treat as Latin: convert any Cyrillic homoglyphs to Latin
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
                d[i - 1][j] + 1,  # deletion
                d[i][j - 1] + 1,  # insertion
                d[i - 1][j - 1] + cost,  # substitution
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


@functools.lru_cache(maxsize=4096)
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


async def _suspend_key_safe(key_hash: str, model: str, category: str, error_text: str) -> None:
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


async def _race_generate(
    target: str,
    guess: str,
    *,
    category: str = "",
    topic_id: str = "",
    sense_context: str | None = None,
) -> GuessJudgement | None:
    """Fire up to 3 Gemini keys simultaneously; return the first valid result.

    Fallback chain:
        1. Race up to 3 keys on _PRIMARY_MODEL (gemini-3.1-flash-lite).
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
    from app.errors import classify_key_error, extract_retry_after_seconds
    from app.metrics import metrics_collector
    from app.providers.gemini import get_cached_genai_client
    from app.repos.keys import get_key_status_manager

    use_case = AgentRequestUseCase()
    status_mgr = get_key_status_manager()
    prompt = _SYSTEM_PROMPT.format(
        W=target,
        G=guess,
        C=(category or "не указана"),
        T=(topic_id or "-"),
        S=(sense_context or category or "не указан"),
    )

    config = _gtypes.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=GuessJudgement.model_json_schema(),
        temperature=0.5,
        max_output_tokens=200,
    )

    async def _one_call(
        api_key: str, key_hash: str, model: str, timeout: float = _LLM_TIMEOUT_S
    ) -> GuessJudgement | None:
        """Single key attempt. On any error: classify → suspend key → return None."""
        lease = await acquire_foreground_slot("judge", "ai_studio", model)
        if lease is None:
            return None
        try:
            async with lease:
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
                submit_task(status_mgr.record_success(key_hash, model))
                submit_task(use_case.increment_key_usage(key_hash, model, use_openrouter=False))
                submit_task(record_result("ai_studio", model, "success"))
                return result
        except Exception as exc:
            err_text = str(exc)
            logger.warning("Judge race call failed (model=%s): %s", model, exc)
            # Classify error and suspend key — prevents the same exhausted key
            # from being re-used in subsequent resolve_ai_request() calls.
            category = classify_key_error(err_text)
            submit_task(
                record_result(
                    "ai_studio",
                    model,
                    category,
                    retry_after_seconds=extract_retry_after_seconds(err_text),
                    reason=err_text[:500],
                )
            )
            submit_task(_suspend_key_safe(key_hash, model, category, err_text[:500]))
            return None
        finally:
            # Record every LLM call attempt in metrics (success OR failure)
            await metrics_collector.record_api_call("gemini_judge", model=model)

    async def _run_race(
        key_pairs: list[tuple[str, str]], model: str, timeout: float = _LLM_TIMEOUT_S
    ) -> GuessJudgement | None:
        """Launch all (api_key, key_hash) pairs concurrently; return first non-None result."""
        tasks = [asyncio.create_task(_one_call(ak, kh, model, timeout)) for ak, kh in key_pairs]
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
            lease = await acquire_foreground_slot("judge", "vertex_express", _PRIMARY_MODEL)
            if lease is None:
                return None
            try:
                async with lease:
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
                    submit_task(record_result("vertex_express", _PRIMARY_MODEL, "success"))
                    return GuessJudgement.model_validate(json.loads(resp_text))
            except Exception as exc:
                logger.debug("Judge: Vertex AI attempt failed: %s", exc)
                submit_task(record_result("vertex_express", _PRIMARY_MODEL, "transient", reason=str(exc)[:500]))
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
            asyncio.create_task(_one_call(ak, kh, resolved_model)) for ak, kh in primary_pairs
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

_HINT_SPACES_RE = re.compile(r"\s+")
_HINT_LIST_STRIP_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|подсказка\s*\d+\s*[:.)-]?)\s*", flags=re.IGNORECASE)
_HINT_FALLBACK_CLEAN_RE = re.compile(r"^(here are the hints|подсказки)\s*:?$", flags=re.IGNORECASE)
_HINT_QUOTED_RE = re.compile(r'"([^"]+)"')
_HINT_SPLIT_RE = re.compile(r"[\s\-]+")


async def generate_hints(word: str, category: str, mode: HintGenerationMode = "foreground") -> list[str]:
    """Generate 3 progressive hints for the given word asynchronously.

    Always returns 3 hints, falling back to deterministic local hints if all
    networked models fail.
    Called from a background asyncio.Task so latency does not block the user.
    """

    def _dedupe_nonempty(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            cleaned = _HINT_SPACES_RE.sub(" ", item).strip().strip("\"'`")
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(cleaned)
        return result

    def _extract_hints(response_text: str) -> list[str]:
        cleaned_text = response_text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()

        def _from_payload(payload: object) -> list[str]:
            if isinstance(payload, dict):
                hints = payload.get("hints", [])
            elif isinstance(payload, list):
                hints = payload
            else:
                hints = []
            if not isinstance(hints, list):
                return []
            return _dedupe_nonempty([str(h).strip() for h in hints if isinstance(h, str | int | float)])

        json_candidates: list[str] = [cleaned_text]
        first_brace = cleaned_text.find("{")
        last_brace = cleaned_text.rfind("}")
        if first_brace != -1 and last_brace > first_brace:
            json_candidates.append(cleaned_text[first_brace : last_brace + 1])
        first_bracket = cleaned_text.find("[")
        last_bracket = cleaned_text.rfind("]")
        if first_bracket != -1 and last_bracket > first_bracket:
            json_candidates.append(cleaned_text[first_bracket : last_bracket + 1])

        for candidate in _dedupe_nonempty(json_candidates):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            hints = _from_payload(parsed)
            if len(hints) >= 3:
                return hints[:3]

        numbered_lines: list[str] = []
        for line in cleaned_text.splitlines():
            stripped = line.strip()
            normalized = _HINT_LIST_STRIP_RE.sub("", line).strip()
            if not normalized:
                continue
            if normalized.lower() == "hints":
                continue
            if stripped.lower().endswith(":") and "hint" in stripped.casefold():
                continue
            if normalized.endswith(":"):
                continue
            if _HINT_FALLBACK_CLEAN_RE.match(normalized):
                continue
            if len(normalized) >= 3:
                numbered_lines.append(normalized)
        numbered_hints = _dedupe_nonempty(numbered_lines)
        if len(numbered_hints) >= 3:
            return numbered_hints[:3]

        quoted = _HINT_QUOTED_RE.findall(cleaned_text)
        quoted_hints = _dedupe_nonempty([q for q in quoted if q.lower() != "hints"])
        if len(quoted_hints) >= 3:
            return quoted_hints[:3]

        return []

    def _local_fallback_hints(target_word: str, target_category: str) -> list[str]:
        compact_word = _HINT_SPACES_RE.sub(" ", target_word).strip()
        tokens = [token for token in _HINT_SPLIT_RE.split(compact_word) if token]
        token_lengths = [sum(1 for ch in token if ch.isalnum()) for token in tokens]
        alnum_chars = [ch for ch in compact_word if ch.isalnum()]
        first_char = alnum_chars[0].upper() if alnum_chars else "?"
        last_char = alnum_chars[-1].upper() if alnum_chars else "?"

        if target_category and "особое" not in target_category.lower():
            first_hint = f"Это из категории «{target_category}»."
        else:
            first_hint = "Слово связано с загаданной темой игры."

        if not token_lengths:
            second_hint = "Подсказка: ответ очень короткий."
        elif len(token_lengths) == 1:
            second_hint = f"Одно слово, {token_lengths[0]} букв."
        else:
            lengths = ", ".join(str(length) for length in token_lengths)
            second_hint = f"{len(token_lengths)} слова; длины: {lengths}."

        if len(token_lengths) <= 1:
            third_hint = f"Первая буква «{first_char}», последняя — «{last_char}»."
        else:
            initials = " ".join(token[0].upper() for token in tokens if token)
            third_hint = f"Начальные буквы слов: {initials}. Последняя — «{last_char}»."

        return [first_hint, second_hint, third_hint]

    def _setting(settings_obj: object | None, name: str, default: Any = None) -> Any:
        if settings_obj is None:
            return default
        return getattr(settings_obj, name, default)

    def _pick_ai_studio_model(settings_obj: object | None) -> str:
        gemini_available = set(_setting(settings_obj, "AVAILABLE_MODELS", []) or [])
        candidates = [
            _HINTS_AI_STUDIO_MODEL,
            _setting(settings_obj, "QNA_MODEL"),
            "gemini-2.5-flash-lite",
            _setting(settings_obj, "DEFAULT_MODEL"),
        ]
        if gemini_available:
            for candidate in candidates:
                if candidate and candidate in gemini_available:
                    return candidate
        for candidate in candidates:
            if candidate:
                return candidate
        return _HINTS_AI_STUDIO_MODEL

    def _pick_opencode_model(settings_obj: object | None) -> str | None:
        opencode_available = set(_setting(settings_obj, "OPENCODE_AVAILABLE_MODELS", []) or [])
        configured_models = [
            _setting(settings_obj, "OPENCODE_QNA_MODEL"),
            _setting(settings_obj, "OPENCODE_DEFAULT_MODEL"),
        ]
        if not opencode_available and not any(configured_models):
            return None

        candidates = [
            _HINTS_OPENCODE_MODEL_CANDIDATES[0],
            _setting(settings_obj, "OPENCODE_QNA_MODEL"),
            *_HINTS_OPENCODE_MODEL_CANDIDATES[1:4],
            _setting(settings_obj, "OPENCODE_DEFAULT_MODEL"),
            _HINTS_OPENCODE_MODEL_CANDIDATES[4],
        ]
        if opencode_available:
            for candidate in candidates:
                if candidate and candidate in opencode_available:
                    return candidate
            return None
        for candidate in candidates:
            if candidate:
                return candidate
        return None

    def _build_hint_lane_plan(settings_obj: object | None) -> list[tuple[str, str, str]]:
        lanes: list[tuple[str, str, str]] = []
        if mode == "foreground":
            lanes.append(("ai_studio", "router", _pick_ai_studio_model(settings_obj)))
        if _setting(settings_obj, "VERTEX_AI_KEY") or _setting(settings_obj, "VERTEX_AI_PROJECT"):
            lanes.append(("vertex_express", "vertex", _HINTS_VERTEX_MODEL))
        opencode_model = _pick_opencode_model(settings_obj)
        if opencode_model:
            lanes.append(("opencode_go", "router", opencode_model))

        deduped: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for lane_name, lane_type, model_name in lanes:
            key = (lane_type, model_name)
            if not model_name or key in seen:
                continue
            seen.add(key)
            deduped.append((lane_name, lane_type, model_name))
        return deduped

    try:
        import app.config as config_module
        from app.errors import classify_key_error, extract_retry_after_seconds, is_error_message, strip_error_tag
        from app.providers import get_provider_router

        c_str = f" (категория: {category})" if category and "особое" not in category.lower() else ""
        prompt = _HINTS_PROMPT.format(W=word, C_STR=c_str)
        settings_obj = getattr(config_module, "settings", None)
        lane_plan = _build_hint_lane_plan(settings_obj)
        router = get_provider_router()
        history = [{"role": "user", "parts": [prompt]}]
        if not lane_plan:
            logger.warning("generate_hints: empty lane plan for word=%r", word)
            fallback_hints = _local_fallback_hints(word, category)
            logger.info("generate_hints: using deterministic fallback for %r", word)
            return fallback_hints

        logger.info(
            "generate_hints: word=%r mode=%s lanes=%s",
            word,
            mode,
            [f"{name}:{model}" for name, _, model in lane_plan],
        )

        def _finalize_hints(response_text: str, lane_name: str, model_name: str) -> list[str] | None:
            if not response_text:
                logger.warning(
                    "generate_hints: empty response from %s lane (%s) for word=%r",
                    lane_name,
                    model_name,
                    word,
                )
                return None

            if is_error_message(response_text):
                logger.warning(
                    "generate_hints: error response from %s lane (%s) for word=%r: %s",
                    lane_name,
                    model_name,
                    word,
                    response_text[:150],
                )
                return None

            hints = _extract_hints(response_text)
            if len(hints) >= 3:
                logger.info(
                    "generate_hints: lane %s (%s) produced valid hints for %r",
                    lane_name,
                    model_name,
                    word,
                )
                return hints[:3]

            logger.warning(
                "generate_hints: no valid hints extracted from %s lane (%s) for word=%r. response prefix: %r",
                lane_name,
                model_name,
                word,
                response_text[:200],
            )
            return None

        async def _one_router_hint_call(lane_name: str, model_name: str) -> list[str] | None:
            provider_name = _budget_provider_for_model(model_name)
            acquire = acquire_background_slot if mode == "background" else acquire_foreground_slot
            lease = await acquire("hint_generation", provider_name, model_name)
            if lease is None:
                return None
            try:
                async with lease:
                    response_text, _ = await router.get_response(
                        preferred_model=model_name,
                        history=history,
                        system_instruction=None,
                        use_openrouter=False,
                        max_key_retries=1,
                        thinking_level="off",
                        timeout=_HINTS_TIMEOUT_S,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await record_result(provider_name, model_name, "transient", reason=str(exc)[:500])
                logger.warning(
                    "generate_hints: router lane %s (%s) failed for %r: %s",
                    lane_name,
                    model_name,
                    word,
                    exc,
                )
                return None
            if is_error_message(response_text):
                await record_result(
                    provider_name,
                    model_name,
                    classify_key_error(response_text),
                    retry_after_seconds=extract_retry_after_seconds(response_text),
                    reason=strip_error_tag(response_text)[:500],
                )
            else:
                await record_result(provider_name, model_name, "success")
            return _finalize_hints(response_text, lane_name, model_name)

        async def _one_vertex_hint_call(model_name: str) -> list[str] | None:
            try:
                from google.genai import types as _gtypes

                from app.providers.gemini import get_vertex_client
            except Exception as exc:
                logger.warning("generate_hints: vertex lane bootstrap failed for %r: %s", word, exc)
                return None

            try:
                vertex_client = get_vertex_client()
            except Exception as exc:
                logger.warning("generate_hints: vertex lane unavailable for %r: %s", word, exc)
                return None

            if vertex_client is None:
                return None

            acquire = acquire_background_slot if mode == "background" else acquire_foreground_slot
            lease = await acquire("hint_generation", "vertex_express", model_name)
            if lease is None:
                return None
            try:
                async with lease:
                    response = await asyncio.wait_for(
                        vertex_client.aio.models.generate_content(
                            model=model_name,
                            contents=prompt,
                            config=_gtypes.GenerateContentConfig(
                                response_mime_type="application/json",
                                response_schema=HintsOutput.model_json_schema(),
                                temperature=0.6,
                                max_output_tokens=220,
                            ),
                        ),
                        timeout=_HINTS_TIMEOUT_S,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await record_result("vertex_express", model_name, "transient", reason=str(exc)[:500])
                logger.warning(
                    "generate_hints: vertex lane (%s) failed for %r: %s",
                    model_name,
                    word,
                    exc,
                )
                return None

            response_text = getattr(response, "text", None) or ""
            if response_text and not is_error_message(response_text):
                await record_result("vertex_express", model_name, "success")
            return _finalize_hints(response_text, "vertex_express", model_name)

        async def _run_lane(lane_name: str, lane_type: str, model_name: str) -> list[str] | None:
            if lane_type == "vertex":
                return await _one_vertex_hint_call(model_name)
            return await _one_router_hint_call(lane_name, model_name)

        if mode == "background":
            for lane_name, lane_type, model_name in lane_plan:
                try:
                    hints = await _run_lane(lane_name, lane_type, model_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "generate_hints: lane %s (%s) crashed for %r: %s",
                        lane_name,
                        model_name,
                        word,
                        exc,
                    )
                    continue
                if hints:
                    return hints
        else:
            tasks = {
                asyncio.create_task(_run_lane(lane_name, lane_type, model_name)): (lane_name, model_name)
                for lane_name, lane_type, model_name in lane_plan
            }
            pending = set(tasks)
            try:
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        lane_name, model_name = tasks[task]
                        try:
                            hints = task.result()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "generate_hints: lane %s (%s) crashed for %r: %s",
                                lane_name,
                                model_name,
                                word,
                                exc,
                            )
                            continue
                        if hints:
                            for other in pending:
                                other.cancel()
                            return hints
            finally:
                for task in pending:
                    task.cancel()

    except Exception:
        logger.exception("generate_hints: unexpected crash for word=%r", word)

    fallback_hints = _local_fallback_hints(word, category)
    logger.info("generate_hints: using deterministic fallback for %r", word)
    return fallback_hints


# ── Score UX helpers ─────────────────────────────────────────────────────────


def score_emoji(score: float) -> str:
    """Return a single emoji representing the semantic temperature of a score.

    Used by callers to prefix the LLM hint text for instant visual feedback
    without modifying the judge's wit (the prompt stays untouched).

    0.0 – 0.3  → 🧊 (cold)
    0.3 – 0.7  → 🟡 (warm)
    0.7 – 0.92 → 🔥 (hot)
    ≥ 0.92     → 🎉 (exact match / synonym — handled by caller as exact_match)
    """
    if score >= 0.92:
        return "🎉"
    if score >= 0.7:
        return "🔥"
    if score >= 0.3:
        return "🟡"
    return "🧊"


def score_bar(score: float, width: int = 10) -> str:
    """Return an ASCII progress bar for the given score (0.0–1.0).

    Example: score_bar(0.6) → "[██████░░░░]"
    """
    filled = round(score * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ── Public entry point ────────────────────────────────────────────────────────


async def judge_guess(
    target: str,
    guess: str,
    *,
    category: str = "",
    topic_id: str = "",
    sense_context: str | None = None,
) -> tuple[str, GuessJudgement]:
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
        await metrics_collector.record_request("judge", time.monotonic() - t0, success=True)
        return "exact_match", j

    # 2. Judgement cache (<5ms, local file)
    cached = await get_cached_judgement(
        target,
        guess,
        category=category,
        topic_id=topic_id,
        sense_context=sense_context,
    )
    if cached is not None:
        cached.cached = True

        # Retroactive fix for already cached hot synonyms:
        if cached.score >= 0.92:
            await metrics_collector.record_request("judge", time.monotonic() - t0, success=True)
            return "exact_match", cached

        await metrics_collector.record_request("judge", time.monotonic() - t0, success=True)
        return cached.status, cached

    # 3. Race×3 LLM
    result = await _race_generate(
        target,
        guess,
        category=category,
        topic_id=topic_id,
        sense_context=sense_context,
    )

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
        await metrics_collector.record_request("judge", elapsed, success=False)
        return "judge_unavailable", sentinel

    status_str: str = result.status
    if result.score >= 0.92:
        status_str = "exact_match"
        result.status = "hot"
        result.score = 1.0
        result.hint = "Угадано! 🎉"

    # Cache result for future identical guesses (fire-and-forget)
    submit_task(
        cache_judgement(
            target,
            guess,
            result,
            category=category,
            topic_id=topic_id,
            sense_context=sense_context,
        )
    )

    await metrics_collector.record_request("judge", elapsed, success=True)
    return status_str, result

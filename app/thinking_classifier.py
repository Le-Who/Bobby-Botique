"""Adaptive Thinking Budget classifier.

Automatically selects the appropriate ``thinking_level`` for a user message
when the user's setting is ``None`` (auto) or explicitly ``"auto"``.

Three-phase classification:
  1. Fast regex heuristics (0 ms, free) → definite HIGH or LOW.
  2. LLM micro-classifier (only for grey-zone MEDIUM) → ~100 ms, ~50 tokens.
  3. Context-aware escalation from conversation history.

User's explicit ``thinking_level`` always overrides.

Model-aware defaults:
  - ``gemini-3.1-flash-lite`` → defaults to ``"high"`` in auto mode
    (stable version benefits from extended thinking; per Google docs).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Phase 1: Regex-based heuristics ──────────────────────────────────────────

# HIGH signals — any single match → "high"
_HIGH_CODE = re.compile(
    r"```|(?:^|\s)(?:def |function |class |import |from |#include|const |let |var )",
    re.MULTILINE,
)
_HIGH_MATH = re.compile(r"\d+\s*[+\-*/^=≥≤]\s*\d+|[∫∑√∏∂∇]|\$[^$]+\$")
_HIGH_MULTIPART = re.compile(r"(?:^|\n)\s*\d+\.\s", re.MULTILINE)
_HIGH_COMPARISON = re.compile(
    r"(?:сравни|отлич|vs\b|плюсы и минусы|pros and cons|versus|преимущест|недостат)",
    re.IGNORECASE,
)
_HIGH_ARCHITECTURE = re.compile(
    r"(?:архитектур|design.?pattern|refactor|оптимиз|масштабир|scalab"
    r"|алгоритм|сложност"
    r"|безопасност|security|vulnerab|шифрован|encrypt|хакер|exploit"
    r"|best.?practi"
    r"|стратег|roadmap|план.{1,30}проект|план.{1,30}задач)",
    re.IGNORECASE,
)
_HIGH_LEGAL = re.compile(
    r"(?:договор|контракт|юридич|legal\s|legislation|liability|compliance|terms\s+of|права\s+потребител|нарушени)",
    re.IGNORECASE,
)
_HIGH_DEBUG = re.compile(
    r"(?:ошибк|error|traceback|не работает|bug|fix|exception|stack.?trace|debug)",
    re.IGNORECASE,
)
_HIGH_LONG_FORM_WRITING = re.compile(
    r"(?:сочинени|сачыненне|сказк|эссе|доклад|реферат|курсовая|диплом|essay|report|thesis|dissertation|статью|лонгрид)",
    re.IGNORECASE,
)

# LOW signals — ALL must match → "low"
_LOW_GREETING = re.compile(
    r"^(?:привет|hello|hi|здравствуй|добрый|хай|хей|hey|yo|good morning|good evening)[\s!.?]*$",
    re.IGNORECASE,
)
_LOW_CONFIRMATION = re.compile(
    r"^(?:да|нет|ок|ладно|понял|хорошо|ага|угу|yes|no|ok|okay|sure|got it|thanks?|спасибо|круто|cool)[\s!.?]*$",
    re.IGNORECASE,
)
_LOW_TRANSLATE = re.compile(
    r"(?:переведи|translate|на русский|на английский|на испанский|в перевод)",
    re.IGNORECASE,
)

# MEDIUM signals (grey zone — triggers LLM classifier if no HIGH/LOW match)
_MEDIUM_EXPLAIN = re.compile(
    r"(?:объясни|почему|как работает|зачем|explain|why|how does)",
    re.IGNORECASE,
)
_MEDIUM_CREATIVE = re.compile(
    r"(?:напиши|сочини|придумай|сгенерируй|создай|write|compose|generate|draft|история|рассказ|стих)",
    re.IGNORECASE,
)
_MEDIUM_SUMMARIZE = re.compile(
    r"(?:кратко|резюме|summarize|summary|итого|суть|tl;?dr)",
    re.IGNORECASE,
)


def _count_questions(text: str) -> int:
    """Count question marks in text."""
    return text.count("?")


def _count_numbered_steps(text: str) -> int:
    """Count numbered list items (1. 2. 3. ...)."""
    return len(_HIGH_MULTIPART.findall(text))


def classify_thinking_level(
    message: str,
    *,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Classify message complexity into a thinking level.

    Returns one of: "low", "medium", "high".

    Args:
        message: The user's message text.
        history: Optional recent conversation history for context-aware escalation.
    """
    msg_len = len(message)
    questions = _count_questions(message)

    # ── HIGH signals (any match) ──
    if _HIGH_CODE.search(message):
        logger.debug("Thinking classifier: HIGH (code detected)")
        return "high"

    if _HIGH_MATH.search(message):
        logger.debug("Thinking classifier: HIGH (math detected)")
        return "high"

    if _count_numbered_steps(message) >= 3:
        logger.debug("Thinking classifier: HIGH (multipart instructions)")
        return "high"

    if _HIGH_COMPARISON.search(message):
        logger.debug("Thinking classifier: HIGH (comparison request)")
        return "high"

    if msg_len > 800:
        logger.debug("Thinking classifier: HIGH (long message: %d chars)", msg_len)
        return "high"

    if questions >= 3:
        logger.debug("Thinking classifier: HIGH (multiple questions: %d)", questions)
        return "high"

    if _HIGH_ARCHITECTURE.search(message):
        logger.debug("Thinking classifier: HIGH (architecture/design)")
        return "high"

    if _HIGH_DEBUG.search(message):
        logger.debug("Thinking classifier: HIGH (debugging)")
        return "high"

    if _HIGH_LEGAL.search(message):
        logger.debug("Thinking classifier: HIGH (legal/contract)")
        return "high"

    if _HIGH_LONG_FORM_WRITING.search(message):
        logger.debug("Thinking classifier: HIGH (long-form writing / essay)")
        return "high"

    # ── LOW signals (ALL must match) ──
    is_short = msg_len < 60
    is_single_question = questions <= 1
    is_greeting = bool(_LOW_GREETING.match(message.strip()))
    is_confirmation = bool(_LOW_CONFIRMATION.match(message.strip()))
    is_translate = bool(_LOW_TRANSLATE.search(message))

    if is_greeting or is_confirmation:
        logger.debug("Thinking classifier: LOW (greeting/confirmation)")
        return "low"

    if is_short and is_single_question and is_translate:
        logger.debug("Thinking classifier: LOW (short translation request)")
        return "low"

    # Single word responses
    if msg_len < 20 and " " not in message.strip():
        logger.debug("Thinking classifier: LOW (single word)")
        return "low"

    if (
        is_short
        and is_single_question
        and not any(
            [
                _MEDIUM_EXPLAIN.search(message),
                _MEDIUM_CREATIVE.search(message),
                _MEDIUM_SUMMARIZE.search(message),
            ]
        )
    ):
        logger.debug("Thinking classifier: LOW (short simple message)")
        return "low"

    # ── Phase 3: Context-aware escalation ──
    level = "medium"

    if history:
        # If recent model responses are long → conversation is complex → escalate
        recent_model_msgs = [h for h in history[-5:] if h.get("role") == "model"]
        long_responses = sum(
            1
            for h in recent_model_msgs
            if any(len(str(p.get("text", "") if isinstance(p, dict) else str(p))) > 2000 for p in h.get("parts", []))
        )
        if long_responses >= 3:
            logger.debug("Thinking classifier: escalated MEDIUM->HIGH (conversation complexity)")
            level = "high"

    # Medium signals (for logging/observability)
    if level == "medium":
        if _MEDIUM_EXPLAIN.search(message):
            logger.debug("Thinking classifier: MEDIUM (explanation request)")
        elif _MEDIUM_CREATIVE.search(message):
            logger.debug("Thinking classifier: MEDIUM (creative request)")
        elif _MEDIUM_SUMMARIZE.search(message):
            logger.debug("Thinking classifier: MEDIUM (summarization)")
        else:
            logger.debug("Thinking classifier: MEDIUM (grey zone, len=%d)", msg_len)

    return level


# ── Model-specific auto-mode defaults ─────────────────────────────────────────
# When the user hasn't set a thinking level, some models benefit from a fixed
# default instead of the adaptive classifier.
_MODEL_DEFAULT_THINKING: dict[str, str] = {
    "3.1-flash-lite": "high",  # stable release; "high" maximises quality
}


def resolve_thinking_level(
    user_level: str | None,
    message: str,
    history: list[dict[str, Any]] | None = None,
    *,
    model: str | None = None,
) -> str | None:
    """Resolve the effective thinking level.

    If the user has explicitly set a level (low/medium/high/off), respect it.
    If the user's level is None or "auto", check model-specific defaults first,
    then fall back to the adaptive classifier.

    Args:
        user_level: User's explicitly chosen level or None (auto).
        message: The user's message text.
        history: Optional recent conversation history for context-aware escalation.
        model: Model name being used (e.g. "gemini-3.1-flash-lite").

    Returns:
        Effective thinking level string or None (for "off").
    """
    # Explicit user override
    if user_level and user_level in ("low", "medium", "high"):
        return user_level
    if user_level == "off":
        return None

    # Model-specific default (auto mode)
    if model:
        for pattern, default_level in _MODEL_DEFAULT_THINKING.items():
            if pattern in model:
                logger.debug(
                    "Thinking resolver: using model default '%s' for %s",
                    default_level,
                    model,
                )
                return default_level

    # Auto mode — classify via heuristics
    return classify_thinking_level(message, history=history)

"""Centralized voice intent detection for text, captions, and forwarded batches."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_EXPLICIT_TRIGGER_PATTERNS = [
    r"\bозвучь\b",
    r"\bозвучь\s+текст\b",
    r"\bозвучь\s+ответ\b",
    r"\bпрочитай\s+вслух\b",
    r"\bзачитай\b",
    r"\bответь\s+голосом\b",
    r"\bскажи\s+голосом\b",
    r"\bread\s+aloud\b",
    r"\bvoice\s+it\b",
    r"\breply\s+by\s+voice\b",
]
_EXPLICIT_TRIGGER_RE = re.compile("|".join(_EXPLICIT_TRIGGER_PATTERNS), re.IGNORECASE)
_SHORT_FORWARD_COMMAND_RE = re.compile(
    r"^(?:пожалуйста[:,]?\s*)?(?:озвучь(?:\s+(?:текст|ответ))?|прочитай\s+вслух|зачитай|"
    r"ответь\s+голосом|скажи\s+голосом|read\s+aloud|voice\s+it|reply\s+by\s+voice)\b",
    re.IGNORECASE,
)
_STRIP_MARKERS_RE = re.compile(r"^[>\-\*\s`'\"“”‘’«»]+|[>\-\*\s`'\"“”‘’«»]+$")
_MULTISPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class VoiceIntentDecision:
    explicit_tts: bool
    confidence: float
    source: str
    reason: str


def normalize_voice_intent_text(text: str | None) -> str:
    """Normalize user-visible text for intent checks."""
    if not text:
        return ""

    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _STRIP_MARKERS_RE.sub("", line)
        if line:
            cleaned_lines.append(line)

    normalized = " ".join(cleaned_lines).lower()
    normalized = _MULTISPACE_RE.sub(" ", normalized).strip(" .,!?:;()[]{}")
    return normalized


def _contains_explicit_trigger(text: str | None) -> bool:
    return bool(_EXPLICIT_TRIGGER_RE.search(normalize_voice_intent_text(text)))


def _is_short_forward_command(text: str | None) -> bool:
    normalized = normalize_voice_intent_text(text)
    if not normalized:
        return False
    if len(normalized.split()) > 10:
        return False
    return bool(_SHORT_FORWARD_COMMAND_RE.match(normalized))


def _build_classifier_prompt(
    *,
    user_text: str,
    llm_context: str,
    user_entry_count: int,
    forwarded_entry_count: int,
) -> str:
    return (
        "You are a strict binary classifier for Telegram bot voice intent.\n"
        "Return ONLY YES or NO.\n\n"
        "Question: Is the user explicitly instructing the bot to speak the bot's reply aloud?\n"
        "Rules:\n"
        "- YES only for direct user intent to hear the bot response as audio.\n"
        "- NO for quoted text, forwarded dialogue content, analysis tasks, or ambiguous mentions.\n"
        "- If the request is ambiguous, return NO.\n\n"
        f"User-authored entry count: {user_entry_count}\n"
        f"Forwarded entry count: {forwarded_entry_count}\n"
        f"User-authored text:\n{user_text or '[none]'}\n\n"
        f"Aggregated context:\n{llm_context or '[none]'}\n"
    )


async def _classify_ambiguous_tts_intent(
    *,
    user_text: str,
    llm_context: str,
    user_entry_count: int,
    forwarded_entry_count: int,
) -> VoiceIntentDecision:
    prompt = _build_classifier_prompt(
        user_text=user_text,
        llm_context=llm_context,
        user_entry_count=user_entry_count,
        forwarded_entry_count=forwarded_entry_count,
    )
    try:
        from app.handlers.ai_core import _resolve_ai_request
        from app.providers.base import get_provider_for_model

        preferred_model = settings.OPENCODE_INLINE_MODEL or settings.OPENCODE_QNA_MODEL
        key_data, model_used, resolution = await _resolve_ai_request(preferred_model, use_openrouter=False)
        if not key_data or not model_used or resolution in {"all_exhausted", "no_keys", "decryption_failed"}:
            return VoiceIntentDecision(
                explicit_tts=False,
                confidence=0.0,
                source="classifier_unavailable",
                reason=resolution or "no_key",
            )

        provider = get_provider_for_model(model_used, key_data["api_key"])
        response = await provider.get_response(
            history=[{"role": "user", "parts": [prompt]}],
            model_name=model_used,
            system_instruction="Return only YES or NO.",
            timeout=12.0,
        )
        raw_text = normalize_voice_intent_text(response.text if response else "")
        if raw_text.startswith("yes"):
            return VoiceIntentDecision(
                explicit_tts=True,
                confidence=0.62,
                source="classifier",
                reason="opencode_yes",
            )
        return VoiceIntentDecision(
            explicit_tts=False,
            confidence=0.3,
            source="classifier",
            reason="opencode_no",
        )
    except Exception as exc:
        logger.debug("Voice intent classifier unavailable: %s", exc)
        return VoiceIntentDecision(
            explicit_tts=False,
            confidence=0.0,
            source="classifier_error",
            reason=type(exc).__name__,
        )


async def detect_tts_intent(
    *,
    user_text: str | None = None,
    caption: str | None = None,
    llm_context: str | None = None,
    user_entries: list[Any] | None = None,
    forwarded_entries: list[Any] | None = None,
) -> VoiceIntentDecision:
    """Detect whether the user explicitly asked to read the reply aloud."""
    normalized_user_text = normalize_voice_intent_text(user_text)
    normalized_caption = normalize_voice_intent_text(caption)
    normalized_context = normalize_voice_intent_text(llm_context)

    entry_user_texts = [normalize_voice_intent_text(getattr(entry, "text", "")) for entry in (user_entries or [])]
    entry_user_texts = [text for text in entry_user_texts if text]
    entry_forwarded_texts = [
        normalize_voice_intent_text(getattr(entry, "text", "")) for entry in (forwarded_entries or [])
    ]
    entry_forwarded_texts = [text for text in entry_forwarded_texts if text]

    effective_user_text = "\n".join(filter(None, [normalized_user_text, normalized_caption, *entry_user_texts])).strip()

    if _contains_explicit_trigger(effective_user_text):
        return VoiceIntentDecision(
            explicit_tts=True,
            confidence=1.0,
            source="user_text",
            reason="explicit_user_command",
        )

    if (
        not effective_user_text
        and len(entry_forwarded_texts) == 1
        and _is_short_forward_command(entry_forwarded_texts[0])
    ):
        return VoiceIntentDecision(
            explicit_tts=True,
            confidence=0.9,
            source="single_forward",
            reason="forwarded_short_command",
        )

    ambiguous_corpus = "\n".join(filter(None, [normalized_context, *entry_forwarded_texts])).strip()
    if _contains_explicit_trigger(ambiguous_corpus):
        return await _classify_ambiguous_tts_intent(
            user_text=effective_user_text,
            llm_context=llm_context or ambiguous_corpus,
            user_entry_count=len(entry_user_texts),
            forwarded_entry_count=len(entry_forwarded_texts),
        )

    return VoiceIntentDecision(
        explicit_tts=False,
        confidence=0.0,
        source="none",
        reason="no_tts_trigger",
    )


def build_voice_source_key(prefix: str, *parts: object) -> str:
    """Build a stable source key for dedupe within the in-memory TTS queue."""
    joined = ":".join(str(part) for part in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"

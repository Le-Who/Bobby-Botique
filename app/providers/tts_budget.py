"""Documented speech token limits and lossless source boundaries."""

import re

from app.providers.tts_audio import TTSRequestError

GEMINI_TTS_INPUT_TOKENS = 8192
GEMINI_TTS_OUTPUT_TOKENS = 16384
_SENTENCE_END = re.compile(r"[.!?…][\"'»”)]*\s+|[。！？][\"'»”」』）]*\s*")


def validate_speech_output_tokens(max_output_tokens: int) -> None:
    """The provider enforces this audio token budget, independently of text size."""
    if not 1024 <= max_output_tokens <= GEMINI_TTS_OUTPUT_TOKENS:
        raise TTSRequestError("invalid_output_budget")


def speech_text_boundaries(text: str) -> list[int]:
    return sorted(
        {
            end
            for pattern in (_SENTENCE_END, re.compile(r"\s+"))
            for match in pattern.finditer(text)
            for end in (match.start(), match.end())
            if 0 < end < len(text) and (end == match.end() or text[end].isspace())
        }
    )


def bisect_speech_text(text: str) -> list[str] | None:
    """Recover incomplete audio at the nearest interior sentence/word boundary."""
    boundaries = speech_text_boundaries(text)
    if not boundaries:
        return None
    middle = min(boundaries, key=lambda end: abs(end - len(text) / 2))
    if not text[:middle].strip() or not text[middle:].strip():
        return None
    return [text[:middle], text[middle:]]

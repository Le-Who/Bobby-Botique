"""Local speech budgets, without a text-model call or tokenizer dependency.

The model card caps Gemini TTS at 8,192 input and 16,384 output tokens.
UTF-8 bytes are a deliberately conservative text-token allowance, never a
claim that characters equal tokens. Reserve another 1,024 tokens for framing.
Audio uses 25 tokens/second; sizing for 8 UTF-8 bytes/second with half that
duration left unused is a narration estimate, not a duration guarantee.
Explicit provider truncation must therefore still cause bounded subdivision.
"""

import re

from app.providers.tts_audio import TTSRequestError

GEMINI_TTS_INPUT_TOKENS = 8192
GEMINI_TTS_INPUT_RESERVE = 1024
GEMINI_TTS_OUTPUT_TOKENS = 16384
_SENTENCE_END = re.compile(r"[.!?…][\"'»”)]*\s+|[。！？][\"'»”」』）]*\s*")


def speech_input_bytes(direction: str, voice: str, language_code: str | None) -> int:
    """Account for every caller-controlled speech metadata string as well."""
    metadata_bytes = len((direction + voice + (language_code or "")).encode("utf-8"))
    remaining = GEMINI_TTS_INPUT_TOKENS - GEMINI_TTS_INPUT_RESERVE - metadata_bytes
    if remaining <= 0:
        raise TTSRequestError("input_too_large")
    return remaining


def speech_output_text_bytes(max_output_tokens: int) -> int:
    """Leave 50% audio-duration headroom at 25 tokens/second and 8 bytes/second."""
    if not 1024 <= max_output_tokens <= GEMINI_TTS_OUTPUT_TOKENS:
        raise TTSRequestError("invalid_output_budget")
    return max_output_tokens * 8 // 50


def split_speech_text(text: str, max_bytes: int) -> list[str]:
    """Keep source text exactly, preferring sentence then whitespace boundaries.

    An oversized lexical unit is rejected visibly instead of speaking fragments
    of a word. We never cut Unicode code points, combining marks or emoji runs.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    chunks: list[str] = []
    remaining = text
    while len(remaining.encode("utf-8")) > max_bytes:
        prefix = remaining.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        sentences = list(_SENTENCE_END.finditer(prefix))
        boundaries = sentences or list(re.finditer(r"\s+", prefix))
        if not boundaries:
            raise TTSRequestError("unsplittable_text")
        end = boundaries[-1].end()
        # A whitespace run can extend beyond the byte prefix, but a lexical
        # unit must never be cut just because its final space is out of range.
        chunks.append(remaining[:end])
        remaining = remaining[end:]
    if remaining:
        chunks.append(remaining)
    return chunks


def bisect_speech_text(text: str) -> list[str] | None:
    """Recover incomplete audio at the nearest interior sentence/word boundary."""
    boundaries = sorted(
        {
            match.end()
            for pattern in (_SENTENCE_END, re.compile(r"\s+"))
            for match in pattern.finditer(text)
            if match.end() < len(text)
        }
    )
    if not boundaries:
        return None
    middle = min(boundaries, key=lambda end: abs(end - len(text) / 2))
    if not text[:middle].strip() or not text[middle:].strip():
        return None
    return [text[:middle], text[middle:]]

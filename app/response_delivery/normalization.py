"""Content normalization shared by streaming drafts and final presentation."""

from __future__ import annotations

import re

_HALLUCINATED_TOOL_INLINE_RE = re.compile(
    r"\[tool_code\]\s*(?:print\()?(?:google_search\.search)\([^)]*\)\)?\s*",
)
_HALLUCINATED_TOOL_LINE_RE = re.compile(
    r"^(?:import google_search|(?:print\()?(?:google_search\.search)\([^)]*\)\)?)\s*$",
)


def strip_hallucinated_tool_trace(text: str) -> str:
    """Remove only leaked Google Search execution traces, preserving normal code."""
    if "[tool_code]" not in text:
        return text

    kept: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "[tool_code]":
            skip = True
            continue
        if skip and (not stripped or _HALLUCINATED_TOOL_LINE_RE.match(stripped)):
            continue
        if skip:
            skip = False
        kept.append(line)
    return _HALLUCINATED_TOOL_INLINE_RE.sub("", "".join(kept))


__all__ = ["strip_hallucinated_tool_trace"]

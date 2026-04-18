# /app/utils/response_tags.py
"""Parse and strip LLM-emitted hidden tags from response text.

Tags handled:
    [VOICE]                  — voice output intent (handled by streaming.py)
    [INTENT:draw|research|tts] — proactive intent routing
    [SUGGESTIONS: s1 | s2 | s3] — smart follow-up suggestions

All parsing is **post-stream**: the full response text is scanned after
streaming completes.  Tags are stripped from the text before it's saved
to history or displayed.
"""

from __future__ import annotations

import hashlib
import re

from cachetools import LRUCache

# 10,000 strings * ~100 bytes = ~1 MB memory footprint
# Survives until process restart. If restarted, old suggestion buttons fail gracefully.
SUGGESTION_CACHE: LRUCache[str, str] = LRUCache(maxsize=10000)

# ── Intent Tag ────────────────────────────────────────────────────────────────
# Matches [INTENT:draw], [INTENT:research], [INTENT:tts] anywhere in the text.
_INTENT_RE = re.compile(r"\[INTENT:(draw|research|tts)\]", re.IGNORECASE)

# Known intent types → button config
INTENT_BUTTONS: dict[str, tuple[str, str]] = {
    "draw": ("🎨 Нарисовать эту сцену?", "intent_route:draw"),
    "research": ("🔬 Глубокий анализ?", "intent_route:research"),
    "tts": ("🎧 Озвучить?", "intent_route:tts"),
}


def extract_intent(text: str) -> tuple[str, str | None]:
    """Extract and strip [INTENT:xxx] tag from response text.

    Returns:
        (cleaned_text, intent_type) where intent_type is 'draw', 'research',
        'tts', or None if no intent tag was found.
    """
    m = _INTENT_RE.search(text)
    if m:
        cleaned = text[: m.start()] + text[m.end() :]
        return cleaned, m.group(1).lower()
    return text, None


# ── Smart Suggestions ─────────────────────────────────────────────────────────
# Matches [SUGGESTIONS: подсказка1 | подсказка2 | подсказка3] anywhere in text.
_SUGGESTIONS_RE = re.compile(
    r"\[SUGGESTIONS:\s*(.+?)\]",
    re.IGNORECASE,
)

# Max suggestions to render as buttons
MAX_SUGGESTIONS = 3
# Label length limit (Telegram UI max display logic)
MAX_SUGGESTION_LABEL_LEN = 100


def extract_suggestions(text: str) -> tuple[str, list[dict[str, str]]]:
    """Extract and strip [SUGGESTIONS: ...] tag from response text.

    Returns:
        (cleaned_text, suggestions_list) where suggestions_list contains
        dicts of {"id": short_hash, "label": visual_text}.
    """
    m = _SUGGESTIONS_RE.search(text)
    if not m:
        return text, []

    raw = m.group(1)
    suggestions = []
    for s in raw.split("|"):
        s = s.strip()
        if not s:
            continue

        full_text = s
        # Truncate visual label just for UI aesthetics, not callback_data limit
        if len(s) > MAX_SUGGESTION_LABEL_LEN:
            s = s[: MAX_SUGGESTION_LABEL_LEN - 3] + "..."

        # Create a short 10-char hash for callback_data
        s_id = hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:10]
        SUGGESTION_CACHE[s_id] = full_text

        suggestions.append({"id": s_id, "label": s})

    suggestions = suggestions[:MAX_SUGGESTIONS]
    cleaned = text[: m.start()] + text[m.end() :]
    return cleaned, suggestions


def parse_response_tags(text: str) -> tuple[str, str | None, list[dict[str, str]]]:
    """Parse all LLM hidden tags from response text in one pass.

    Returns:
        (cleaned_text, intent_type, suggestions)
    """
    # Suggestions first (they're usually at the very end before footers)
    text, suggestions = extract_suggestions(text)
    # Then intent tag
    text, intent = extract_intent(text)

    # Cleanup any excessive blank lines left behind by tag extraction
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, intent, suggestions


# ── Code Block Extraction ─────────────────────────────────────────────────────
# Matches fenced code blocks: ```lang\ncode\n```
_CODE_BLOCK_RE = re.compile(r"```(?:\w*)\n(.*?)```", re.DOTALL)

# Minimum code block length to offer CopyTextButton (skip trivial snippets)
_MIN_CODE_BLOCK_LEN = 20


def extract_first_code_block(text: str) -> str | None:
    """Extract the first significant fenced code block from response text.

    Returns the code content (without fences) if found and >= 20 chars,
    or None if no code block is present.  Used to offer a CopyTextButton.
    """
    m = _CODE_BLOCK_RE.search(text)
    if m:
        code = m.group(1).strip()
        if len(code) >= _MIN_CODE_BLOCK_LEN:
            return code
    return None

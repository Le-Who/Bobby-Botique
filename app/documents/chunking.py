"""Document chunking for retrieval-time context assembly.

No schema change — documents stay as full text in `user_documents.content`.
Chunking is applied at retrieval time when injecting document context into
chat prompts, so the LLM receives the most relevant portions within the
token budget.

Three strategies:
- `recursive_chunk`: Split text by paragraph → sentence → token boundary.
- `hierarchical_chunk`: Parent/child chunks for long documents (>5000 tokens).
- `chunk_for_context`: Main entry point — picks strategy, scores relevance,
  and returns the best chunks that fit within the token budget.
"""

import re
from dataclasses import dataclass

# Approximate characters per token for mixed Cyrillic/Latin text
_CHARS_PER_TOKEN = 3.5


def _estimate_tokens(text: str) -> int:
    """Fast token estimate without calling a tokenizer."""
    return int(len(text) / _CHARS_PER_TOKEN)


# ── Recursive chunking ──────────────────────────────────────────────────


def recursive_chunk(
    text: str,
    max_tokens: int = 512,
    overlap_ratio: float = 0.15,
) -> list[str]:
    """Split text recursively by \\n\\n → \\n → '. ' → token boundary.

    Returns chunks with ~15% overlap for context continuity.
    """
    if not text or not text.strip():
        return []

    max_chars = int(max_tokens * _CHARS_PER_TOKEN)
    overlap_chars = int(max_chars * overlap_ratio)

    # If text fits in one chunk, return as-is
    if len(text) <= max_chars:
        return [text.strip()]

    # Try splitting by hierarchy of separators
    for separator in ["\n\n", "\n", ". ", " "]:
        parts = text.split(separator)
        if len(parts) > 1:
            chunks = _merge_parts(parts, separator, max_chars, overlap_chars)
            if chunks:
                return chunks

    # Fallback: hard split at max_chars boundaries
    return _hard_split(text, max_chars, overlap_chars)


def _merge_parts(
    parts: list[str],
    separator: str,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Merge small parts into chunks up to max_chars, with overlap."""
    chunks: list[str] = []
    current = ""

    for part in parts:
        candidate = (current + separator + part) if current else part

        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            # Start new chunk with overlap from previous
            if chunks and overlap_chars > 0:
                prev = chunks[-1]
                overlap = prev[-overlap_chars:]
                current = overlap + separator + part
            else:
                current = part

            # If single part exceeds max_chars, split it further
            if len(current) > max_chars:
                sub_chunks = _hard_split(current, max_chars, overlap_chars)
                chunks.extend(sub_chunks[:-1])
                current = sub_chunks[-1] if sub_chunks else ""

    if current and current.strip():
        chunks.append(current.strip())

    return chunks


def _hard_split(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """Split text at exact character boundaries with overlap."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap_chars if end < len(text) else end
    return chunks


# ── Hierarchical chunking ────────────────────────────────────────────────


@dataclass
class HierarchicalChunk:
    """A parent chunk containing child chunks for relevance scoring."""
    parent: str
    children: list[str]


def hierarchical_chunk(
    text: str,
    child_tokens: int = 256,
    parent_tokens: int = 1024,
) -> list[HierarchicalChunk]:
    """For documents >5000 tokens. Returns parent/child structure.

    Children are used for relevance scoring against the query.
    Parents are used for context injection (broader context).
    """
    if not text or not text.strip():
        return []

    # First split into parent-sized chunks
    parent_chunks = recursive_chunk(text, max_tokens=parent_tokens, overlap_ratio=0.1)

    result: list[HierarchicalChunk] = []
    for parent_text in parent_chunks:
        children = recursive_chunk(parent_text, max_tokens=child_tokens, overlap_ratio=0.1)
        result.append(HierarchicalChunk(parent=parent_text, children=children))

    return result


# ── Relevance scoring ────────────────────────────────────────────────────


def _score_chunk(chunk: str, query: str) -> float:
    """Simple keyword overlap score. Returns 0.0 to 1.0."""
    if not query or not query.strip():
        return 0.5  # No query → equal relevance for all chunks

    # Normalize
    chunk_lower = chunk.lower()
    query_words = set(re.findall(r"\w{3,}", query.lower()))  # 3+ char words

    if not query_words:
        return 0.5

    # Count matches
    matches = sum(1 for w in query_words if w in chunk_lower)
    return matches / len(query_words)


# ── Main entry point ─────────────────────────────────────────────────────


def chunk_for_context(
    text: str,
    query: str = "",
    max_context_tokens: int = 4000,
) -> str:
    """Given full document text and optional query, return most relevant
    chunks that fit within max_context_tokens.

    For short documents (< max_context_tokens), returns the full text.
    For long documents, uses hierarchical chunking + relevance scoring.
    """
    if not text or not text.strip():
        return ""

    text = text.strip()

    # Short document → return as-is
    doc_tokens = _estimate_tokens(text)
    if doc_tokens <= max_context_tokens:
        return text

    # Long document → hierarchical chunk + score + select
    h_chunks = hierarchical_chunk(text)

    if not h_chunks:
        # Fallback: truncate to budget
        max_chars = int(max_context_tokens * _CHARS_PER_TOKEN)
        return text[:max_chars].strip()

    # Score each parent by best child score
    scored: list[tuple[float, HierarchicalChunk]] = []
    for hc in h_chunks:
        if not query:
            # No query: use sequential order (score by position, first chunks higher)
            scored.append((0.5, hc))
        else:
            best_child_score = max(_score_chunk(c, query) for c in hc.children) if hc.children else 0.0
            scored.append((best_child_score, hc))

    # Sort by score (highest first), then original order for ties
    scored.sort(key=lambda x: -x[0])

    # Assemble context within budget
    max_chars = int(max_context_tokens * _CHARS_PER_TOKEN)
    selected: list[str] = []
    chars_used = 0

    for _score, hc in scored:
        parent_len = len(hc.parent)
        if chars_used + parent_len + 10 > max_chars:  # +10 for separator
            break
        selected.append(hc.parent)
        chars_used += parent_len + 2  # \n\n separator

    if not selected:
        # At least include a truncated version of the top chunk
        selected.append(scored[0][1].parent[:max_chars])

    return "\n\n".join(selected)

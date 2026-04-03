# /app/repos/memory_config.py
"""Shared constants for the long-term memory subsystem.

Centralizes configuration that is referenced by multiple modules
(memory.py, memory_consolidation.py, handlers) to prevent drift.
"""

# ── Embedding Configuration ─────────────────────────────────────────────────
EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

# ── Storage Limits ───────────────────────────────────────────────────────────
MAX_MEMORIES_PER_USER = 500
DEFAULT_MEMORY_TTL_DAYS = 90

# ── Retrieval ────────────────────────────────────────────────────────────────
# Model used for multi-query expansion (~200ms cheap call)
QUERY_EXPANSION_MODEL = "gemini-3.1-flash-lite-preview"

# ── Consolidation ────────────────────────────────────────────────────────────
# Approximate tokens per character for mixed Cyrillic/Latin text
CHARS_PER_TOKEN = 3.5
# Token threshold to trigger consolidation
CONSOLIDATION_TOKEN_THRESHOLD = 8000
# Days since last consolidation to trigger temporal consolidation
CONSOLIDATION_TEMPORAL_DAYS = 7
# LLM persona fact extraction limits
MAX_PERSONA_FACTS = 8
MIN_PERSONA_FACTS = 5
# Consolidation model — cheapest available free-tier model
CONSOLIDATION_MODEL = "gemini-3.1-flash-lite-preview"

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
QUERY_EXPANSION_MODEL = "gemini-3.1-flash-lite"

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
CONSOLIDATION_MODEL = "gemini-3.1-flash-lite"

# ── Real-Time Graph Extraction ───────────────────────────────────────────────
# Model for real-time entity/relation extraction from user messages.
# gemini-3-flash-preview: reliable last-stand. flash-lite is often offline.
# Key rotation in extract_graph_structured() handles per-key 503s automatically.
GRAPH_EXTRACTION_MODEL = "gemini-3-flash-preview"
# ThinkingConfig level — disabled (only applies to pro/think variants)
GRAPH_EXTRACTION_THINKING_LEVEL = ""
# Minimum user message length to qualify for graph extraction
MIN_EXTRACTION_LENGTH = 30

# ── MemPalace Wing/Room Taxonomy ─────────────────────────────────────────────
TAXONOMY_WINGS = ("identity", "projects", "social", "knowledge", "temporal")
TAXONOMY_ROOMS = {
    "identity": ("bio", "prefs", "health", "skills", "values"),
    "projects": ("active", "archived", "ideas"),
    "social": ("family", "friends", "colleagues", "contacts"),
    "knowledge": ("tech", "science", "culture", "languages"),
    "temporal": ("events", "plans", "routines", "milestones"),
}
TAXONOMY_HALL_TYPES = ("fact", "opinion", "event", "plan", "preference", "habit")

# Model for taxonomy classification is admin-configurable via env TAXONOMY_MODEL
# or runtime via config_manager.update_setting("TAXONOMY_MODEL", "new-model-name").
# Defaults to gemini-3.1-flash-lite (cheap, fast, sufficient for classification).
_TAXONOMY_MODEL_FALLBACK = "gemini-3.1-flash-lite"


def get_taxonomy_model() -> str:
    """Return the taxonomy classification model from live config (hot-reloadable)."""
    try:
        from app.config import config_manager

        return config_manager.get_setting("TAXONOMY_MODEL", _TAXONOMY_MODEL_FALLBACK)
    except Exception:
        return _TAXONOMY_MODEL_FALLBACK


# ── AAAK Tiered Compression ──────────────────────────────────────────────────
# L0: Core facts (always injected, compact JSON)
L0_MAX_TOKENS = 250
# L1: Active context (structured summary from recent LTM + role diaries)
L1_MAX_TOKENS = 600
# L2: Semantic recall (search_memories_with_graph results)
L2_MAX_TOKENS = 1500
# L3: Full history (assembler.py handles this via existing token budget)

# ── Edge Provenance (HippoRAG 2) ──────────────────────────────────────────────
# Maximum characters per source passage injected alongside graph triples
SOURCE_PASSAGE_MAX_CHARS = 200
# Number of top-weighted edges that get source passage surfacing
SOURCE_PASSAGE_TOP_K = 3

# ── Role Diaries ─────────────────────────────────────────────────────────────
MAX_DIARY_ENTRIES_PER_ROLE = 20
DIARY_ENTRY_MAX_LENGTH = 500

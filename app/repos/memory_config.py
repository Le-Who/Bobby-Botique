"""Shared constants for the long-term memory subsystem.

Centralizes configuration that is referenced by multiple modules
(memory.py, memory_consolidation.py, handlers) to prevent drift.
"""

EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIMENSION = 768

MAX_MEMORIES_PER_USER = 500
DEFAULT_MEMORY_TTL_DAYS = 90

QUERY_EXPANSION_MODEL = "gemini-3.5-flash-lite"

CHARS_PER_TOKEN = 3.5
CONSOLIDATION_TOKEN_THRESHOLD = 8000
CONSOLIDATION_TEMPORAL_DAYS = 7
MAX_PERSONA_FACTS = 8
MIN_PERSONA_FACTS = 5
CONSOLIDATION_MODEL = "gemini-3.5-flash-lite"

GRAPH_EXTRACTION_MODEL = "gemini-3.6-flash"
GRAPH_EXTRACTION_THINKING_LEVEL = ""
MIN_EXTRACTION_LENGTH = 30

TAXONOMY_WINGS = ("identity", "projects", "social", "knowledge", "temporal")
TAXONOMY_ROOMS = {
    "identity": ("bio", "prefs", "health", "skills", "values"),
    "projects": ("active", "archived", "ideas"),
    "social": ("family", "friends", "colleagues", "contacts"),
    "knowledge": ("tech", "science", "culture", "languages"),
    "temporal": ("events", "plans", "routines", "milestones"),
}
TAXONOMY_HALL_TYPES = ("fact", "opinion", "event", "plan", "preference", "habit")

_TAXONOMY_MODEL_FALLBACK = "gemini-3.5-flash-lite"


def get_taxonomy_model() -> str:
    """Return the taxonomy classification model from live config (hot-reloadable)."""
    try:
        from app.config import config_manager

        return config_manager.get_setting("TAXONOMY_MODEL", _TAXONOMY_MODEL_FALLBACK)
    except Exception:
        return _TAXONOMY_MODEL_FALLBACK


L0_MAX_TOKENS = 250
L1_MAX_TOKENS = 600
L2_MAX_TOKENS = 1500

SOURCE_PASSAGE_MAX_CHARS = 200
SOURCE_PASSAGE_TOP_K = 3

MAX_DIARY_ENTRIES_PER_ROLE = 20
DIARY_ENTRY_MAX_LENGTH = 500

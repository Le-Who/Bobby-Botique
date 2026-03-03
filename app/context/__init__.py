"""
Context assembly package — token-budget-aware LLM prompt building.

Re-exports everything for backward compatibility with::

    from app.context_assembler import ContextAssembler, get_assembler, ...
"""

# --- Data structures & constants ---
# --- Core assembler ---
from app.context.assembler import (  # noqa: F401
    ContextAssembler,
    get_assembler,
    should_summarize,
)

# --- Summarizer ---
from app.context.summarizer import (  # noqa: F401
    schedule_llm_summarization,
    split_into_chunks,
)
from app.context.token_budget import (  # noqa: F401
    CHUNK_SIZE,
    DEFAULT_TOKEN_BUDGET,
    LLM_SUMMARY_TOKEN_THRESHOLD,
    MAX_CHUNKS,
    MIN_HISTORY_MESSAGES,
    RESPONSE_RESERVE,
    SUMMARIZATION_MODEL,
    SUMMARY_BUDGET,
    AssembledContext,
    TokenBudget,
)

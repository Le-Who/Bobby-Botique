"""
Context assembly package — token-budget-aware LLM prompt building.

Re-exports everything for backward compatibility with::

    from app.context_assembler import ContextAssembler, get_assembler, ...
"""

# --- Data structures & constants ---
from app.context.token_budget import (  # noqa: F401
    DEFAULT_TOKEN_BUDGET,
    RESPONSE_RESERVE,
    SUMMARY_BUDGET,
    MIN_HISTORY_MESSAGES,
    LLM_SUMMARY_TOKEN_THRESHOLD,
    CHUNK_SIZE,
    MAX_CHUNKS,
    SUMMARIZATION_MODEL,
    TokenBudget,
    AssembledContext,
)

# --- Summarizer ---
from app.context.summarizer import (  # noqa: F401
    schedule_llm_summarization,
    split_into_chunks,
)

# --- Core assembler ---
from app.context.assembler import (  # noqa: F401
    ContextAssembler,
    get_assembler,
    should_summarize,
)

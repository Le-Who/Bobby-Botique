"""
Token budget configuration and data structures for context assembly.

Constants and dataclasses that define the token budget allocation
for LLM requests.
"""

from dataclasses import dataclass, field
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic

# ============================================================================
# TOKEN BUDGET CONFIGURATION
# ============================================================================

# Context cap at 128K — Flash models degrade beyond this (research-backed)
DEFAULT_TOKEN_BUDGET = 128_000
RESPONSE_RESERVE = 12_000  # 3 full Telegram messages (4K chars each)
SUMMARY_BUDGET = 4_000  # Rich summaries for novel-writing use cases
MIN_HISTORY_MESSAGES = 4  # Always keep at least this many recent messages

# Summarization thresholds
LLM_SUMMARY_TOKEN_THRESHOLD = 30_000  # Min dropped tokens to trigger LLM tier
CHUNK_SIZE = 10_000  # Tokens per chunk for refine-chain summarization
MAX_CHUNKS = 6  # Max chunks per summarization (cost control)
SUMMARIZATION_MODEL = "gemini-3.1-flash-lite"


@dataclass
class TokenBudget:
    """Token allocation across context layers."""

    total: int = DEFAULT_TOKEN_BUDGET
    system_prompt: int = 0  # Filled after system prompt is resolved
    summary: int = 0  # Filled if summary exists
    history: int = 0  # Computed as remainder
    user_message: int = 0  # Filled from current message
    response_reserve: int = RESPONSE_RESERVE

    @property
    def available_for_history(self) -> int:
        """Tokens available for conversation history."""
        used = self.system_prompt + self.summary + self.user_message + self.response_reserve
        return max(0, self.total - used)

    @property
    def used(self) -> int:
        """Total tokens allocated across all prompt and response layers."""
        return self.system_prompt + self.summary + self.history + self.user_message + self.response_reserve


def truncate_to_token_budget(text: str, token_limit: int, suffix: str = "...") -> str:
    """Truncate UTF-8 text without exceeding the project's token estimator.

    Character slicing is unsafe for Cyrillic and emoji because the estimator is
    byte based.  Reserve room for the suffix, then back off to a valid UTF-8
    boundary so the returned value is always within ``token_limit``.
    """
    if token_limit < 0:
        raise ValueError("token_limit must be non-negative")
    if estimate_tokens_cyrillic(text) <= token_limit:
        return text
    if token_limit == 0:
        return ""

    suffix_bytes = suffix.encode("utf-8")
    byte_budget = token_limit * 3
    if len(suffix_bytes) > byte_budget:
        suffix = ""
        suffix_bytes = b""

    encoded = text.encode("utf-8")
    end = min(len(encoded), byte_budget - len(suffix_bytes))
    while end:
        try:
            prefix = encoded[:end].decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            end = exc.start
    else:
        prefix = ""

    result = prefix + suffix
    # The estimator uses floor division, but keep this invariant explicit in
    # case its implementation changes independently.
    while result and estimate_tokens_cyrillic(result) > token_limit:
        prefix = prefix[:-1]
        result = prefix + suffix
    return result


@dataclass
class AssembledContext:
    """Result of context assembly — ready to send to the LLM."""

    history: list[dict[str, Any]]
    system_instruction: str
    # Canonical persisted turns after trimming.  Unlike ``history`` this never
    # contains the synthetic summary exchange or the current request.
    retained_history: list[dict[str, Any]] = field(default_factory=list)
    summary: str | None = None
    budget: TokenBudget = field(default_factory=TokenBudget)
    was_truncated: bool = False
    dropped_messages: list[dict[str, Any]] = field(default_factory=list)
    messages_dropped: int = 0
    audit_hash: str = ""  # Hash of assembled prompt for debugging
    llm_summarization_scheduled: bool = False  # True if async LLM summary was fired

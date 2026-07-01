"""
Token budget configuration and data structures for context assembly.

Constants and dataclasses that define the token budget allocation
for LLM requests.
"""

from dataclasses import dataclass, field
from typing import Any

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


@dataclass
class AssembledContext:
    """Result of context assembly — ready to send to the LLM."""

    history: list[dict[str, Any]]
    system_instruction: str
    summary: str | None = None
    budget: TokenBudget = field(default_factory=TokenBudget)
    was_truncated: bool = False
    dropped_messages: list[dict[str, Any]] = field(default_factory=list)
    messages_dropped: int = 0
    audit_hash: str = ""  # Hash of assembled prompt for debugging
    llm_summarization_scheduled: bool = False  # True if async LLM summary was fired

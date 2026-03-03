"""Tests for app.context.token_budget — data structures and constants."""

from app.context.token_budget import (
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

# ── TokenBudget dataclass ────────────────────────────────────────────────────


class TestTokenBudget:
    def test_defaults(self):
        budget = TokenBudget()
        assert budget.total == DEFAULT_TOKEN_BUDGET
        assert budget.system_prompt == 0
        assert budget.summary == 0
        assert budget.history == 0
        assert budget.user_message == 0
        assert budget.response_reserve == RESPONSE_RESERVE

    def test_available_for_history_calculation(self):
        budget = TokenBudget(
            total=10000,
            system_prompt=2000,
            summary=500,
            user_message=1000,
            response_reserve=3000,
        )
        # 10000 - 2000 - 500 - 1000 - 3000 = 3500
        assert budget.available_for_history == 3500

    def test_available_never_negative(self):
        budget = TokenBudget(
            total=100,
            system_prompt=100,
            summary=100,
            user_message=100,
            response_reserve=100,
        )
        assert budget.available_for_history == 0

    def test_custom_total(self):
        budget = TokenBudget(total=64_000)
        assert budget.available_for_history == 64_000 - RESPONSE_RESERVE


# ── AssembledContext dataclass ───────────────────────────────────────────────


class TestAssembledContext:
    def test_defaults(self):
        ctx = AssembledContext(
            history=[],
            system_instruction="test",
        )
        assert ctx.summary is None
        assert ctx.was_truncated is False
        assert ctx.dropped_messages == []
        assert ctx.messages_dropped == 0
        assert ctx.audit_hash == ""
        assert ctx.llm_summarization_scheduled is False

    def test_with_truncation(self):
        ctx = AssembledContext(
            history=[{"role": "user", "parts": ["hi"]}],
            system_instruction="test",
            was_truncated=True,
            messages_dropped=5,
            summary="Dropped context summary",
        )
        assert ctx.was_truncated is True
        assert ctx.messages_dropped == 5
        assert ctx.summary == "Dropped context summary"


# ── Constants consistency ────────────────────────────────────────────────────


class TestConstantsConsistency:
    def test_response_reserve_less_than_budget(self):
        assert RESPONSE_RESERVE < DEFAULT_TOKEN_BUDGET

    def test_summary_budget_less_than_response_reserve(self):
        assert SUMMARY_BUDGET < RESPONSE_RESERVE

    def test_llm_threshold_reasonable(self):
        """LLM summary threshold should be much larger than summary budget."""
        assert LLM_SUMMARY_TOKEN_THRESHOLD > SUMMARY_BUDGET

    def test_chunk_limits(self):
        """Total chunked tokens (MAX_CHUNKS * CHUNK_SIZE) should be reasonable."""
        assert MAX_CHUNKS * CHUNK_SIZE <= DEFAULT_TOKEN_BUDGET

    def test_min_history_positive(self):
        assert MIN_HISTORY_MESSAGES > 0

    def test_summarization_model_is_lite(self):
        """Cheapest model should be used for summarization."""
        assert "lite" in SUMMARIZATION_MODEL.lower()

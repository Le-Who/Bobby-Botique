"""Tests for app.context_assembler — token-budget-aware context assembly."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.context_assembler import (
    CHUNK_SIZE,
    DEFAULT_TOKEN_BUDGET,
    LLM_SUMMARY_TOKEN_THRESHOLD,
    MAX_CHUNKS,
    MIN_HISTORY_MESSAGES,
    RESPONSE_RESERVE,
    SUMMARIZATION_MODEL,
    SUMMARY_BUDGET,
    AssembledContext,
    ContextAssembler,
    TokenBudget,
    get_assembler,
    should_summarize,
)
from app.prompt_registry import estimate_tokens_cyrillic

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_msg(role: str, text: str) -> dict:
    """Create a message dict."""
    return {"role": role, "parts": [text]}


def make_history(n: int, msg_size: int = 50) -> list[dict]:
    """Create a history with n alternating user/model messages."""
    history = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "model"
        text = f"Message {i}: " + "x" * msg_size
        history.append(make_msg(role, text))
    return history


# ── Constants ─────────────────────────────────────────────────────────────────


class TestConstants:
    """Verify the research-backed constants are correctly configured."""

    def test_budget_is_128k(self):
        assert DEFAULT_TOKEN_BUDGET == 128_000

    def test_response_reserve_is_12k(self):
        assert RESPONSE_RESERVE == 12_000

    def test_summary_budget_is_4k(self):
        assert SUMMARY_BUDGET == 4_000

    def test_chunk_size_is_10k(self):
        assert CHUNK_SIZE == 10_000

    def test_max_chunks_is_6(self):
        assert MAX_CHUNKS == 6

    def test_llm_threshold_is_30k(self):
        assert LLM_SUMMARY_TOKEN_THRESHOLD == 30_000

    def test_summarization_model(self):
        assert SUMMARIZATION_MODEL == "gemini-2.5-flash-lite"


# ── TokenBudget ───────────────────────────────────────────────────────────────


class TestTokenBudget:
    def test_available_for_history(self):
        budget = TokenBudget(
            total=10000,
            system_prompt=1000,
            summary=200,
            user_message=500,
            response_reserve=2000,
        )
        assert budget.available_for_history == 10000 - 1000 - 200 - 500 - 2000

    def test_available_never_negative(self):
        budget = TokenBudget(
            total=100,
            system_prompt=50,
            summary=50,
            user_message=50,
            response_reserve=50,
        )
        assert budget.available_for_history >= 0


# ── ContextAssembler — basic ──────────────────────────────────────────────────


class TestContextAssemblerBasic:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_empty_history(self):
        result = self.assembler.assemble(
            history=[],
            user_message="Hello",
            system_instruction="Be helpful",
        )
        assert isinstance(result, AssembledContext)
        assert len(result.history) == 1  # Just the user message
        assert result.history[0]["role"] == "user"
        assert not result.was_truncated
        assert result.messages_dropped == 0

    def test_small_history_no_truncation(self):
        history = [
            make_msg("user", "Hi"),
            make_msg("model", "Hello!"),
        ]
        result = self.assembler.assemble(
            history=history,
            user_message="How are you?",
            system_instruction="Be helpful",
        )
        # Should include all history + current message
        assert not result.was_truncated
        assert result.messages_dropped == 0
        # History + user message
        assert len(result.history) == 3

    def test_user_message_appended(self):
        result = self.assembler.assemble(
            history=[make_msg("user", "First"), make_msg("model", "Answer")],
            user_message="Follow up",
            system_instruction="Be helpful",
        )
        last_msg = result.history[-1]
        assert last_msg["role"] == "user"
        assert "Follow up" in last_msg["parts"][0]

    def test_system_instruction_preserved(self):
        result = self.assembler.assemble(
            history=[],
            user_message="Hi",
            system_instruction="Custom system prompt",
        )
        assert result.system_instruction == "Custom system prompt"

    def test_llm_summarization_not_scheduled_for_small(self):
        """LLM summarization should NOT trigger for small truncations."""
        result = self.assembler.assemble(
            history=make_history(10, msg_size=50),
            user_message="Q",
            system_instruction="S",
            token_budget=500,
        )
        # Even if truncated, dropped tokens < 30K → no LLM scheduling
        assert not result.llm_summarization_scheduled


# ── ContextAssembler — truncation ─────────────────────────────────────────────


class TestContextAssemblerTruncation:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_truncates_when_over_budget(self):
        # Create a history that exceeds a small budget
        history = make_history(20, msg_size=200)
        result = self.assembler.assemble(
            history=history,
            user_message="Question",
            system_instruction="Be helpful",
            token_budget=500,  # Very small budget
        )
        assert result.was_truncated
        assert result.messages_dropped > 0
        assert len(result.history) < len(history) + 1

    def test_keeps_minimum_messages(self):
        history = make_history(20, msg_size=500)
        result = self.assembler.assemble(
            history=history,
            user_message="Q",
            system_instruction="S",
            token_budget=100,  # Extremely tight
        )
        # Should still keep at least MIN_HISTORY_MESSAGES
        total_msgs = len(result.history)
        assert total_msgs >= MIN_HISTORY_MESSAGES

    def test_creates_summary_on_truncation(self):
        history = make_history(20, msg_size=200)
        result = self.assembler.assemble(
            history=history,
            user_message="Q",
            system_instruction="S",
            token_budget=500,
        )
        assert result.summary is not None
        assert len(result.summary) > 0

    def test_summary_respects_budget(self):
        history = make_history(50, msg_size=300)
        result = self.assembler.assemble(
            history=history,
            user_message="Q",
            system_instruction="S",
            token_budget=1000,
        )
        if result.summary:
            summary_tokens = estimate_tokens_cyrillic(result.summary)
            assert summary_tokens <= SUMMARY_BUDGET + 50  # Small tolerance

    def test_llm_scheduled_for_large_drops(self):
        """When >30K tokens are dropped, LLM summarization should be flagged."""
        # Create messages totaling >30K tokens
        # Each message ~1000 tokens → 40 messages = ~40K tokens
        history = make_history(40, msg_size=3000)
        result = self.assembler.assemble(
            history=history,
            user_message="Q",
            system_instruction="S",
            token_budget=2000,  # Tight budget → most messages dropped
        )
        assert result.was_truncated
        assert result.llm_summarization_scheduled


# ── ContextAssembler — summary integration ────────────────────────────────────


class TestContextAssemblerSummary:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_existing_summary_preserved(self):
        result = self.assembler.assemble(
            history=[make_msg("user", "Hi"), make_msg("model", "Hello")],
            user_message="Next",
            system_instruction="S",
            existing_summary="Previous context about Python",
        )
        # Summary should appear in the history
        found = any(
            "контекст" in str(msg.get("parts", "")).lower()
            for msg in result.history
        )
        assert found

    def test_summary_not_as_orphan_user_message(self):
        """Summary should be inserted as user/model pair, not orphan user."""
        result = self.assembler.assemble(
            history=[make_msg("user", "Hi"), make_msg("model", "Hello")],
            user_message="Next",
            system_instruction="S",
            existing_summary="Context summary",
        )
        # Check that the summary user message is followed by model acknowledgment
        for i, msg in enumerate(result.history):
            if "Контекст предыдущей беседы" in str(msg.get("parts", "")):
                assert i + 1 < len(result.history)
                assert result.history[i + 1]["role"] == "model"
                break


# ── ContextAssembler — role alternation ───────────────────────────────────────


class TestRoleAlternation:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_consecutive_same_role_merged(self):
        history = [
            make_msg("user", "Part 1"),
            make_msg("user", "Part 2"),
            make_msg("model", "Response"),
        ]
        result = self.assembler.assemble(
            history=history,
            user_message="Next",
            system_instruction="S",
        )
        # Check no consecutive same roles
        for i in range(1, len(result.history)):
            assert result.history[i]["role"] != result.history[i - 1]["role"], (
                f"Consecutive same roles at index {i-1} and {i}: "
                f"{result.history[i-1]['role']}"
            )

    def test_proper_alternation_with_summary(self):
        result = self.assembler.assemble(
            history=[make_msg("user", "Hi"), make_msg("model", "Hello")],
            user_message="Next",
            system_instruction="S",
            existing_summary="Old context",
        )
        for i in range(1, len(result.history)):
            assert result.history[i]["role"] != result.history[i - 1]["role"]


# ── Chunk splitting ───────────────────────────────────────────────────────────


class TestChunkSplitting:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_empty_messages_no_chunks(self):
        chunks = self.assembler._split_into_chunks([])
        assert chunks == []

    def test_single_small_message(self):
        msgs = [make_msg("user", "Hello world")]
        chunks = self.assembler._split_into_chunks(msgs)
        assert len(chunks) == 1
        assert "Hello world" in chunks[0]

    def test_chunk_contains_role_labels(self):
        msgs = [
            make_msg("user", "Question"),
            make_msg("model", "Answer"),
        ]
        chunks = self.assembler._split_into_chunks(msgs)
        assert len(chunks) >= 1
        assert "user:" in chunks[0]
        assert "model:" in chunks[0]

    def test_respects_max_chunks(self):
        # Create many large messages that would exceed MAX_CHUNKS
        msgs = make_history(100, msg_size=5000)
        chunks = self.assembler._split_into_chunks(msgs)
        assert len(chunks) <= MAX_CHUNKS

    def test_splits_at_chunk_size(self):
        # Each message ~500 tokens, CHUNK_SIZE=10K → ~20 msgs per chunk
        msgs = make_history(60, msg_size=1500)
        chunks = self.assembler._split_into_chunks(msgs)
        assert len(chunks) > 1  # Should split into multiple chunks


# ── LLM summarization scheduling ─────────────────────────────────────────────


class TestLLMSummarizationScheduling:
    def setup_method(self):
        self.assembler = ContextAssembler()

    @pytest.mark.asyncio
    async def test_schedule_creates_task(self):
        """schedule_llm_summarization should create a background task."""
        callback = AsyncMock()
        dropped = make_history(20, msg_size=500)

        # Use the event loop from the async test
        with patch.object(
            self.assembler,
            "_run_llm_summarization",
            new_callable=AsyncMock,
        ):
            self.assembler.schedule_llm_summarization(dropped, None, callback)
            # Allow the task to start
            await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_refine_chain_calls_llm(self):
        """_run_llm_summarization should call the LLM for each chunk."""
        callback = AsyncMock()
        # 2 messages, each small enough for 1 chunk
        dropped = [
            make_msg("user", "What is Python?"),
            make_msg("model", "Python is a language."),
        ]

        with patch(
            "app.context_assembler.ContextAssembler._split_into_chunks",
            return_value=["user: What is Python?\nmodel: Python is a language."],
        ):
            with patch(
                "app.handlers.ai_core._get_ai_response_with_routing",
                new_callable=AsyncMock,
                return_value="## Факты\n- Python — это язык программирования",
            ) as mock_llm:
                await self.assembler._run_llm_summarization(
                    dropped, None, callback
                )
                mock_llm.assert_called_once()
                callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_refine_chain_sequential(self):
        """Refine chain should pass previous summary to next chunk."""
        callback = AsyncMock()
        dropped = make_history(6, msg_size=500)

        call_count = 0
        responses = [
            "## Факты\n- Первый факт",
            "## Факты\n- Первый факт\n- Второй факт",
        ]

        async def mock_llm(*args, **kwargs):
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return responses[idx]

        with patch(
            "app.context_assembler.ContextAssembler._split_into_chunks",
            return_value=["chunk1_text", "chunk2_text"],
        ):
            with patch(
                "app.handlers.ai_core._get_ai_response_with_routing",
                side_effect=mock_llm,
            ):
                await self.assembler._run_llm_summarization(
                    dropped, None, callback
                )
                assert call_count == 2  # Two chunks → two LLM calls
                callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure_handled_gracefully(self):
        """If LLM call fails, should log error and not crash."""
        callback = AsyncMock()
        dropped = [make_msg("user", "test")]

        with patch(
            "app.context_assembler.ContextAssembler._split_into_chunks",
            return_value=["test chunk"],
        ):
            with patch(
                "app.handlers.ai_core._get_ai_response_with_routing",
                new_callable=AsyncMock,
                side_effect=Exception("API Error"),
            ):
                # Should not raise
                await self.assembler._run_llm_summarization(
                    dropped, None, callback
                )
                callback.assert_not_called()


# ── ContextAssembler — audit ──────────────────────────────────────────────────


class TestAudit:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_audit_hash_generated(self):
        result = self.assembler.assemble(
            history=[], user_message="Hi", system_instruction="S"
        )
        assert result.audit_hash
        assert len(result.audit_hash) == 12

    def test_audit_hash_deterministic(self):
        kwargs = dict(history=[], user_message="Hi", system_instruction="S")
        result1 = self.assembler.assemble(**kwargs)
        result2 = self.assembler.assemble(**kwargs)
        assert result1.audit_hash == result2.audit_hash

    def test_audit_hash_changes_with_input(self):
        result1 = self.assembler.assemble(
            history=[], user_message="Hi", system_instruction="S"
        )
        result2 = self.assembler.assemble(
            history=[], user_message="Bye", system_instruction="S"
        )
        assert result1.audit_hash != result2.audit_hash


# ── should_summarize ──────────────────────────────────────────────────────────


class TestShouldSummarize:
    def test_empty_history(self):
        should, reason = should_summarize([])
        assert not should

    def test_small_history(self):
        history = make_history(5, msg_size=20)
        should, reason = should_summarize(history)
        assert not should

    def test_many_messages_triggers(self):
        history = make_history(60, msg_size=20)  # Over 50 message soft limit
        should, reason = should_summarize(history)
        assert should
        assert "50" in reason

    def test_high_tokens_triggers(self):
        # Create messages with lots of text
        history = make_history(10, msg_size=50000)
        should, reason = should_summarize(history, token_budget=10000)
        assert should
        assert "token" in reason.lower() or "Token" in reason


# ── TokenBudget in assembled result ───────────────────────────────────────────


class TestBudgetAccounting:
    def setup_method(self):
        self.assembler = ContextAssembler()

    def test_budget_accounts_system_prompt(self):
        system = "A" * 300
        result = self.assembler.assemble(
            history=[], user_message="Hi", system_instruction=system
        )
        assert result.budget.system_prompt > 0
        assert result.budget.system_prompt == estimate_tokens_cyrillic(system)

    def test_budget_accounts_user_message(self):
        msg = "A long user message " * 20
        result = self.assembler.assemble(
            history=[], user_message=msg, system_instruction="S"
        )
        assert result.budget.user_message > 0
        assert result.budget.user_message == estimate_tokens_cyrillic(msg)


# ── Singleton ─────────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_assembler_returns_same_instance(self):
        a1 = get_assembler()
        a2 = get_assembler()
        assert a1 is a2

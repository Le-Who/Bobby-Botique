"""
Core context assembler that builds token-budget-aware LLM prompts.

This is the main orchestration layer: it allocates the token budget,
trims history, delegates to the local or LLM summarizer, and produces
an ``AssembledContext`` ready for the provider.
"""

import hashlib
import logging
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic

from .summarizer import (
    _extract_text as extract_text,  # canonical implementation
    schedule_llm_summarization,  # noqa: F401 – re-exported
)
from .token_budget import (
    DEFAULT_TOKEN_BUDGET,
    LLM_SUMMARY_TOKEN_THRESHOLD,
    MIN_HISTORY_MESSAGES,
    RESPONSE_RESERVE,
    SUMMARY_BUDGET,
    AssembledContext,
    TokenBudget,
)

logger = logging.getLogger(__name__)


class ContextAssembler:
    """Assembles LLM context with token-budget awareness.

    Usage::

        assembler = ContextAssembler()
        result = assembler.assemble(
            history=chat_state.history,
            user_message="Hello",
            system_instruction="You are helpful...",
            token_budget=128000,
        )
        # result.history — trimmed history ready for LLM
        # result.system_instruction — the system prompt
        # result.summary — summary of dropped messages (if any)
    """

    def assemble(
        self,
        history: list[dict[str, Any]],
        user_message: str,
        system_instruction: str,
        existing_summary: str | None = None,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
    ) -> AssembledContext:
        """Assemble context within token budget.

        Args:
            history: Full conversation history (list of {role, parts} dicts).
            user_message: Current user message.
            system_instruction: System prompt text.
            existing_summary: Previously generated summary (if any).
            token_budget: Total token budget for the request.

        Returns:
            AssembledContext with trimmed history and metadata.
        """
        budget = TokenBudget(total=token_budget)

        # 1. Account for fixed costs
        budget.system_prompt = estimate_tokens_cyrillic(system_instruction)
        budget.user_message = estimate_tokens_cyrillic(user_message)

        # 2. Check if summary exists and account for it
        if existing_summary:
            budget.summary = estimate_tokens_cyrillic(existing_summary)

        # 3. Fit history within remaining budget
        available = budget.available_for_history
        trimmed_history, was_truncated, dropped_count, new_summary = self._fit_history(
            history, available, existing_summary
        )

        if new_summary and new_summary != existing_summary:
            budget.summary = estimate_tokens_cyrillic(new_summary)

        # 4. Determine dropped messages and if we should schedule LLM summarization
        llm_scheduled = False
        dropped_msgs: list[dict[str, Any]] = []
        if was_truncated:
            # Compute what was dropped (first N messages not in trimmed)
            kept_count = len(trimmed_history)
            dropped_msgs = list(history[: len(history) - kept_count])
            dropped_tokens = sum(
                estimate_tokens_cyrillic(self._extract_text(msg))
                for msg in dropped_msgs
            )
            if dropped_tokens >= LLM_SUMMARY_TOKEN_THRESHOLD:
                llm_scheduled = True

        # 5. Build final history with proper structure
        final_history = self._build_final_history(
            trimmed_history, new_summary, user_message
        )

        budget.history = sum(
            estimate_tokens_cyrillic(self._extract_text(msg))
            for msg in trimmed_history
        )

        # 6. Generate audit hash
        audit_hash = self._compute_audit_hash(final_history, system_instruction)

        return AssembledContext(
            history=final_history,
            system_instruction=system_instruction,
            summary=new_summary,
            budget=budget,
            was_truncated=was_truncated,
            dropped_messages=dropped_msgs,
            messages_dropped=dropped_count,
            audit_hash=audit_hash,
            llm_summarization_scheduled=llm_scheduled,
        )

    def _fit_history(
        self,
        history: list[dict[str, Any]],
        available_tokens: int,
        existing_summary: str | None,
    ) -> tuple[list[dict[str, Any]], bool, int, str | None]:
        """Fit history within available token budget.

        Strategy:
        - Keep the most recent messages (they're most relevant).
        - If history exceeds budget, drop oldest messages.
        - Create a text summary of dropped messages.

        Returns:
            (trimmed_history, was_truncated, dropped_count, summary)
        """
        if not history:
            return [], False, 0, existing_summary

        # Calculate total tokens for all history messages
        message_tokens = []
        for msg in history:
            text = self._extract_text(msg)
            tokens = estimate_tokens_cyrillic(text)
            message_tokens.append(tokens)

        total_history_tokens = sum(message_tokens)

        # If everything fits, no truncation needed
        if total_history_tokens <= available_tokens:
            return list(history), False, 0, existing_summary

        # Need to truncate — keep most recent messages
        # Work backwards from the end, accumulating tokens
        kept_indices = []
        accumulated = 0
        for i in range(len(history) - 1, -1, -1):
            msg_tokens = message_tokens[i]
            if accumulated + msg_tokens > available_tokens and len(kept_indices) >= MIN_HISTORY_MESSAGES:
                break
            accumulated += msg_tokens
            kept_indices.append(i)

        kept_indices.reverse()

        # Determine dropped messages
        if kept_indices:
            first_kept = kept_indices[0]
        else:
            first_kept = len(history)

        dropped_messages = history[:first_kept]
        trimmed_history = [history[i] for i in kept_indices]
        dropped_count = len(dropped_messages)

        # Create local summary of dropped messages (immediate, non-blocking)
        summary = self._create_summary_local(dropped_messages, existing_summary)

        logger.info(
            "Context trimmed: dropped %d messages, kept %d, saved ~%d tokens",
            dropped_count,
            len(trimmed_history),
            total_history_tokens - accumulated,
        )

        return trimmed_history, True, dropped_count, summary

    # ── Local summarization (Tier 1: fast, no API cost) ──────────────────────

    def _create_summary_local(
        self,
        dropped_messages: list[dict[str, Any]],
        existing_summary: str | None,
    ) -> str | None:
        """Create a local text summary of dropped messages (no LLM).

        Used for immediate response when LLM summarization hasn't completed yet,
        or when the dropped content is small (<30K tokens).
        """
        if not dropped_messages:
            return existing_summary

        # Extract snippets from dropped messages
        user_questions: list[str] = []
        assistant_points: list[str] = []

        for msg in dropped_messages:
            role = msg.get("role", "")
            text = self._extract_text(msg)

            if not text:
                continue

            # Keep first 150 chars of each message for context
            snippet = text[:150].strip()
            if len(text) > 150:
                snippet += "..."

            if role == "user":
                user_questions.append(snippet)
            elif role == "model":
                assistant_points.append(snippet)

        # Build structured summary
        parts: list[str] = []

        if existing_summary:
            parts.append(existing_summary.strip())

        if user_questions:
            # Keep last 5 questions to avoid summary bloat
            recent_questions = user_questions[-5:]
            parts.append("Темы пользователя: " + " | ".join(recent_questions))

        if assistant_points:
            # Keep last 3 assistant points
            recent_points = assistant_points[-3:]
            parts.append("Ключевые ответы: " + " | ".join(recent_points))

        summary = "\n".join(parts) if parts else None

        # Cap summary length to stay within budget
        if summary and estimate_tokens_cyrillic(summary) > SUMMARY_BUDGET:
            max_chars = SUMMARY_BUDGET * 2  # Rough chars-to-tokens for Cyrillic
            summary = summary[:max_chars] + "..."

        return summary

    # ── History building ─────────────────────────────────────────────────────

    def _build_final_history(
        self,
        trimmed_history: list[dict[str, Any]],
        summary: str | None,
        user_message: str,
    ) -> list[dict[str, Any]]:
        """Build the final message list for the LLM.

        Summary is inserted as a user/model pair at the top to maintain
        proper role alternation for Gemini.
        """
        context: list[dict[str, Any]] = []

        # Insert summary as context preamble (user/model pair)
        if summary:
            context.append({
                "role": "user",
                "parts": [f"[Контекст предыдущей беседы]\n{summary}"],
            })
            context.append({
                "role": "model",
                "parts": ["Понял, учитываю контекст предыдущей беседы."],
            })

        # Add trimmed history
        context.extend(trimmed_history)

        # Add current user message (always append to guarantee non-empty history)
        msg_text = user_message.strip() if user_message else ""
        context.append({"role": "user", "parts": [msg_text or "..."]})

        # Validate role alternation
        context = self._fix_role_alternation(context)

        return context

    def _fix_role_alternation(
        self, history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Ensure proper user/model role alternation.

        Gemini requires strict alternation. This merges consecutive
        same-role messages.
        """
        if len(history) <= 1:
            return history

        fixed: list[dict[str, Any]] = [history[0]]
        for msg in history[1:]:
            if msg.get("role") == fixed[-1].get("role"):
                # Merge consecutive same-role messages
                existing_parts = fixed[-1].get("parts", [])
                new_parts = msg.get("parts", [])
                fixed[-1]["parts"] = existing_parts + new_parts
            else:
                fixed.append(msg)

        return fixed

    # ── Utilities ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(msg: dict[str, Any]) -> str:
        """Extract text content from a message dict."""
        return extract_text(msg)

    def _compute_audit_hash(
        self, history: list[dict[str, Any]], system_instruction: str
    ) -> str:
        """Compute a hash of the assembled prompt for audit/debugging."""
        content = system_instruction
        for msg in history:
            content += f"|{msg.get('role', '')}:{self._extract_text(msg)}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def should_summarize(
    history: list[dict[str, Any]],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    system_prompt_tokens: int = 0,
) -> tuple[bool, str]:
    """Check if history needs summarization.

    Returns:
        (should_summarize, reason)
    """
    if not history:
        return False, ""

    # Count tokens in history
    total_tokens = sum(
        estimate_tokens_cyrillic(_extract_msg_text(msg))
        for msg in history
    )

    available = token_budget - system_prompt_tokens - RESPONSE_RESERVE
    threshold = int(available * 0.8)  # Trigger at 80% utilization

    if total_tokens > threshold:
        return True, f"History tokens ({total_tokens}) exceed 80% of available ({threshold})"

    if len(history) > 50:
        return True, f"Message count ({len(history)}) exceeds soft limit (50)"

    return False, ""


# Use the shared implementation from summarizer
_extract_msg_text = extract_text


# Singleton assembler instance
_assembler = ContextAssembler()


def get_assembler() -> ContextAssembler:
    """Get the global ContextAssembler instance."""
    return _assembler

# /app/context_assembler.py
"""
Token-budget-aware context assembler for LLM requests.

Replaces the naive context management in prompts.py with a structured,
layered approach that respects token budgets and provides high-quality
LLM-based summarization for long conversations.

Design:
    Total token budget: 128K (capped at Flash model's effective range).
    Layers:
    1. System prompt (fixed, ~300-1500 tokens)
    2. Summary of old context (if any, up to ~4000 tokens)
    3. History window (variable, fills remaining budget, capped at ~110K)
    4. Current user message
    5. Response reserve (~12000 tokens for 3 Telegram messages)

Summarization:
    Two-tier approach:
    - Local (snippet extraction): for small drops (<30K tokens)
    - LLM (refine-chain):        for large drops (≥30K tokens)
    The LLM summarization runs async in the background, never blocking
    the current user request. Results are used on the NEXT request.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic

logger = logging.getLogger(__name__)

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
SUMMARIZATION_MODEL = "gemini-2.5-flash-lite"  # Cheapest, 84.1% FACTS grounding


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


# ============================================================================
# CONTEXT ASSEMBLY RESULT
# ============================================================================

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


# ============================================================================
# CONTEXT ASSEMBLER
# ============================================================================

class ContextAssembler:
    """Assembles LLM context with token-budget awareness.

    Usage:
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

    # ── LLM summarization (Tier 2: high-quality, async) ──────────────────────

    def schedule_llm_summarization(
        self,
        dropped_messages: list[dict[str, Any]],
        existing_summary: str | None,
        callback,
    ) -> None:
        """Schedule async LLM summarization as a background task.

        The callback receives the finished summary string and should store it
        in chat_state._context_summary for the NEXT request.

        Args:
            dropped_messages: Messages dropped from history.
            existing_summary: Previous summary (to merge with).
            callback: async callable(summary: str) to store the result.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._run_llm_summarization(dropped_messages, existing_summary, callback)
            )
            logger.info(
                "LLM summarization scheduled for %d dropped messages",
                len(dropped_messages),
            )
        except RuntimeError:
            logger.warning(
                "No running event loop — cannot schedule LLM summarization"
            )

    async def _run_llm_summarization(
        self,
        dropped_messages: list[dict[str, Any]],
        existing_summary: str | None,
        callback,
    ) -> None:
        """Run refine-chain LLM summarization in background.

        Strategy:
        1. Split dropped messages into ~10K token chunks
        2. Refine sequentially: chunk₁ → summary → refined with chunk₂ → ...
        3. Call callback with final summary
        """
        try:
            # Build text representation of dropped messages
            chunks = self._split_into_chunks(dropped_messages)

            if not chunks:
                if callback and existing_summary:
                    await callback(existing_summary)
                return

            logger.info(
                "Starting refine-chain summarization: %d chunks", len(chunks)
            )

            # Import here to avoid circular imports
            from app.prompt_registry import (
                SUMMARIZATION_CHUNK,
                SUMMARIZATION_REFINE_FIRST,
                SUMMARIZATION_REFINE_SUBSEQUENT,
                SUMMARIZATION_SYSTEM,
            )

            # Calculate per-chunk summary token target
            max_tokens_per_chunk = max(500, SUMMARY_BUDGET // max(len(chunks), 1))

            summary = existing_summary or ""

            for i, chunk_text in enumerate(chunks):
                # Build refine instruction
                if i == 0 and not summary:
                    refine_instruction = SUMMARIZATION_REFINE_FIRST
                else:
                    refine_instruction = SUMMARIZATION_REFINE_SUBSEQUENT.replace(
                        "{previous_summary}", summary
                    )

                # Build the prompt from template
                prompt_text = SUMMARIZATION_CHUNK.text
                prompt_text = prompt_text.replace("{refine_instruction}", refine_instruction)
                prompt_text = prompt_text.replace("{max_tokens}", str(max_tokens_per_chunk))
                prompt_text = prompt_text.replace("{conversation_chunk}", chunk_text)

                # Call LLM
                from app.handlers.ai_core import _get_ai_response_with_routing

                summary = await _get_ai_response_with_routing(
                    preferred_model=SUMMARIZATION_MODEL,
                    history=[{"role": "user", "parts": [prompt_text]}],
                    system_instruction=SUMMARIZATION_SYSTEM.text,
                )

                logger.info(
                    "Refine chain step %d/%d complete, summary ~%d tokens",
                    i + 1,
                    len(chunks),
                    estimate_tokens_cyrillic(summary) if summary else 0,
                )

            # Cap to budget
            if summary and estimate_tokens_cyrillic(summary) > SUMMARY_BUDGET:
                max_chars = SUMMARY_BUDGET * 2
                summary = summary[:max_chars] + "..."

            if callback and summary:
                await callback(summary)
                logger.info("LLM summarization complete, callback executed")

        except Exception:
            logger.exception("LLM summarization failed — local summary will be used")

    def _split_into_chunks(
        self, messages: list[dict[str, Any]]
    ) -> list[str]:
        """Split messages into ~CHUNK_SIZE token chunks for refine-chain.

        Each chunk is a text block of consecutive messages, preserving
        role labels for context.
        """
        if not messages:
            return []

        chunks: list[str] = []
        current_chunk_parts: list[str] = []
        current_tokens = 0

        for msg in messages:
            role = msg.get("role", "unknown")
            text = self._extract_text(msg)
            if not text:
                continue

            line = f"{role}: {text}"
            line_tokens = estimate_tokens_cyrillic(line)

            if current_tokens + line_tokens > CHUNK_SIZE and current_chunk_parts:
                chunks.append("\n".join(current_chunk_parts))
                current_chunk_parts = []
                current_tokens = 0

                # Respect MAX_CHUNKS limit
                if len(chunks) >= MAX_CHUNKS:
                    break

            current_chunk_parts.append(line)
            current_tokens += line_tokens

        # Don't forget the last chunk
        if current_chunk_parts and len(chunks) < MAX_CHUNKS:
            chunks.append("\n".join(current_chunk_parts))

        return chunks

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

        # Add current user message
        if user_message:
            context.append({"role": "user", "parts": [user_message]})

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

    def _extract_text(self, msg: dict[str, Any]) -> str:
        """Extract text content from a message dict."""
        parts = msg.get("parts", [])
        if not parts:
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(str(p) for p in content)
            return str(content)

        text_parts: list[str] = []
        for part in parts:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                text_parts.append(part["text"])
        return " ".join(text_parts)

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


def _extract_msg_text(msg: dict[str, Any]) -> str:
    """Quick text extraction for token counting."""
    parts = msg.get("parts", [])
    if isinstance(parts, list):
        return " ".join(str(p) for p in parts if isinstance(p, str))
    return str(msg.get("content", ""))


# Singleton assembler instance
_assembler = ContextAssembler()


def get_assembler() -> ContextAssembler:
    """Get the global ContextAssembler instance."""
    return _assembler

"""
LLM-based refine-chain summarization for context assembly.

Tier 2 summarization: schedules a background Gemini call to produce
high-quality summaries of dropped conversation history.
"""

import asyncio
import logging
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic

from .token_budget import (
    CHUNK_SIZE,
    MAX_CHUNKS,
    SUMMARIZATION_MODEL,
    SUMMARY_BUDGET,
    truncate_to_token_budget,
)

logger = logging.getLogger(__name__)

# Keep strong references until every user-scoped task reaches a terminal state.
# A set (rather than a single task) is intentional: a superseded task remains
# tracked while cancellation propagates through the provider client.
_summarization_tasks_by_user: dict[int, set[asyncio.Task[None]]] = {}


class SummarizationInputTooLarge(ValueError):
    """Raised when safe bounded summarization would exceed its API-call budget."""


def schedule_llm_summarization(
    user_id: int,
    dropped_messages: list[dict[str, Any]],
    existing_summary: str | None,
    callback,
    *,
    expected_epoch: int | None = None,
) -> asyncio.Task[None] | None:
    """Schedule async LLM summarization as a background task.

    The callback receives the finished summary string and should store it
    in chat_state._context_summary for the NEXT request.

    Args:
        user_id: Owner of the history, used for lifecycle cancellation.
        dropped_messages: Messages dropped from history.
        existing_summary: Previous summary (to merge with).
        callback: async callable(summary: str) to store the result.

    Returns:
        The tracked task, or ``None`` when called without a running loop.
    """
    try:
        loop = asyncio.get_running_loop()

        # A newer summary makes every older in-flight refinement stale.  Keep
        # cancelled tasks in the registry until their cancellation completes.
        user_tasks = _summarization_tasks_by_user.setdefault(user_id, set())
        for previous in tuple(user_tasks):
            if not previous.done():
                previous.cancel()

        task = loop.create_task(
            _run_llm_summarization(
                user_id,
                expected_epoch,
                dropped_messages,
                existing_summary,
                callback,
            )
        )
        user_tasks.add(task)

        def _discard_finished(done: asyncio.Task[None]) -> None:
            tracked = _summarization_tasks_by_user.get(user_id)
            if tracked is None:
                return
            tracked.discard(done)
            if not tracked:
                _summarization_tasks_by_user.pop(user_id, None)

        task.add_done_callback(_discard_finished)
        logger.info(
            "LLM summarization scheduled for user %d (%d dropped messages)",
            user_id,
            len(dropped_messages),
        )
        return task
    except RuntimeError:
        logger.warning("No running event loop — cannot schedule LLM summarization")
        return None


async def cancel_user_summarization_tasks(user_id: int) -> int:
    """Cancel and await every locally tracked summarization for ``user_id``."""
    current = asyncio.current_task()
    tracked = _summarization_tasks_by_user.get(user_id, set())
    tasks = [task for task in tuple(tracked) if task is not current and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(tasks)


async def _run_llm_summarization(
    user_id: int,
    expected_epoch: int | None,
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
        try:
            chunks = split_into_chunks(dropped_messages)
        except SummarizationInputTooLarge:
            # The synchronous local summary is already persisted by the
            # assembler.  Do not replace it with a partial LLM summary or send
            # an oversized request to the provider.
            logger.warning(
                "Skipping external summarization: input exceeds %d chunks of %d tokens",
                MAX_CHUNKS,
                CHUNK_SIZE,
            )
            return

        if not chunks:
            if callback and existing_summary:
                await callback(existing_summary)
            return

        logger.info("Starting refine-chain summarization: %d chunks", len(chunks))

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
                refine_instruction = SUMMARIZATION_REFINE_SUBSEQUENT.replace("{previous_summary}", summary)

            # Build the prompt from template
            prompt_text = SUMMARIZATION_CHUNK.text
            prompt_text = prompt_text.replace("{refine_instruction}", refine_instruction)
            prompt_text = prompt_text.replace("{max_tokens}", str(max_tokens_per_chunk))
            prompt_text = prompt_text.replace("{conversation_chunk}", chunk_text)

            # Call LLM
            from app.handlers.ai_core import _get_ai_response_with_routing
            from app.repos.memory_consent import private_data_lease

            async with private_data_lease(
                user_id,
                expected_epoch,
                purpose="conversation:summary",
                require_ltm=False,
            ) as lease_acquired:
                if not lease_acquired:
                    logger.info("Skipped stale account summary for user %d", user_id)
                    return
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
            summary = truncate_to_token_budget(summary, SUMMARY_BUDGET)

        if callback and summary:
            await callback(summary)
            logger.info("LLM summarization complete, callback executed")

    except Exception:
        logger.exception("LLM summarization failed — local summary will be used")


def split_into_chunks(messages: list[dict[str, Any]]) -> list[str]:
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
        text = _extract_text(msg)
        if not text:
            continue

        line = f"{role}: {text}"
        for fragment in _split_text_to_token_limit(line, CHUNK_SIZE):
            fragment_tokens = estimate_tokens_cyrillic(fragment)
            if current_chunk_parts and current_tokens + fragment_tokens > CHUNK_SIZE:
                chunks.append("\n".join(current_chunk_parts))
                if len(chunks) >= MAX_CHUNKS:
                    raise SummarizationInputTooLarge
                current_chunk_parts = []
                current_tokens = 0

            current_chunk_parts.append(fragment)
            current_tokens += fragment_tokens

            if current_tokens >= CHUNK_SIZE:
                chunks.append("\n".join(current_chunk_parts))
                if len(chunks) > MAX_CHUNKS:
                    raise SummarizationInputTooLarge
                current_chunk_parts = []
                current_tokens = 0

    # Don't forget the last chunk
    if current_chunk_parts:
        chunks.append("\n".join(current_chunk_parts))
    if len(chunks) > MAX_CHUNKS:
        raise SummarizationInputTooLarge

    return chunks


def _split_text_to_token_limit(text: str, token_limit: int) -> list[str]:
    """Split UTF-8 text so every fragment stays within the estimator limit."""
    if token_limit < 1:
        raise ValueError("token_limit must be positive")

    remaining = text.encode("utf-8")
    max_bytes = token_limit * 3
    fragments: list[str] = []
    while remaining:
        end = min(len(remaining), max_bytes)
        while end:
            try:
                fragment = remaining[:end].decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                end = exc.start
        if not end:
            raise ValueError("could not split UTF-8 summarization input")
        fragments.append(fragment)
        remaining = remaining[end:]
    return fragments


def _extract_text(msg: dict[str, Any]) -> str:
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

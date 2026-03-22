"""
LLM-based refine-chain summarization for context assembly.

Tier 2 summarization: schedules a background Gemini call to produce
high-quality summaries of dropped conversation history.
"""

import asyncio
import logging
from typing import Any

from app.prompt_registry import estimate_tokens_cyrillic

from .token_budget import CHUNK_SIZE, MAX_CHUNKS, SUMMARIZATION_MODEL, SUMMARY_BUDGET

logger = logging.getLogger(__name__)


def schedule_llm_summarization(
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
        task = loop.create_task(_run_llm_summarization(dropped_messages, existing_summary, callback))
        # prevent GC of fire-and-forget task
        task.add_done_callback(lambda t: None)
        logger.info(
            "LLM summarization scheduled for %d dropped messages",
            len(dropped_messages),
        )
    except RuntimeError:
        logger.warning("No running event loop — cannot schedule LLM summarization")


async def _run_llm_summarization(
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
        chunks = split_into_chunks(dropped_messages)

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


def _extract_text(msg: dict[str, Any]) -> str:
    """Extract text content from a message dict."""
    parts = msg.get("parts", [])
    if not parts:
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        parts = content if isinstance(content, list) else [content]

    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, (bytes, bytearray)):
            continue
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            # Bolt optimization: prevent O(N) memory allocation by skipping massive base64/image payloads
            if "inline_data" in part or "image_url" in part:
                continue
            if "text" in part:
                text_parts.append(str(part["text"]))
        else:
            text_parts.append(str(part))
    return " ".join(text_parts)

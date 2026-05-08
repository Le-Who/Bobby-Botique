"""
Pure-logic helpers extracted from _handle_regular_chat.

These functions contain **zero I/O** — no Telegram, DB, or API calls.
They can be tested directly without mocking.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ─── Resolution classification ──────────────────────────────────────────────


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of classifying a model resolution attempt."""

    action: str  # "proceed" | "all_exhausted" | "confirm_fallback"
    provider_name: str | None = None  # "Gemini" | "OpenRouter"
    fallback_model: str | None = None
    user_message: str | None = None


def classify_resolution(
    resolution: str,
    model_requested: str | None,
    model_fallback: str | None = None,
) -> ResolutionResult:
    """
    Classify the AI key-resolution outcome into an action.

    Pure function — no I/O. Just branching logic.

    Args:
        resolution: The resolution string from _resolve_ai_request.
        model_requested: The model the user wanted.
        model_fallback: The fallback model offered (if any).

    Returns:
        ResolutionResult with action and optional metadata.
    """
    if resolution == "all_exhausted":
        from app.providers.base import is_freetheai_model, is_opencode_model, is_openrouter_model

        if model_requested and is_opencode_model(model_requested):
            provider = "Opencode Go"
        elif model_requested and is_freetheai_model(model_requested):
            provider = "FreeTheAI"
        elif model_requested and is_openrouter_model(model_requested):
            provider = "OpenRouter"
        else:
            provider = "Gemini"
        return ResolutionResult(
            action="all_exhausted",
            provider_name=provider,
            user_message=(f"🚫 Все лимиты для всех моделей {provider} на сегодня исчерпаны. Попробуйте позже."),
        )

    if resolution == "decryption_failed":
        return ResolutionResult(
            action="decryption_failed",
            user_message="⚠️ Системная ошибка: Не удалось расшифровать API-ключи. Пожалуйста, проверьте конфигурацию `ADMIN_SECRET`."
        )

    if resolution == "confirm_fallback":
        return ResolutionResult(
            action="confirm_fallback",
            fallback_model=model_fallback,
            user_message=(
                f"Все лимиты для модели `{model_requested}` на сегодня исчерпаны.\n"
                f"Однако, я могу выполнить ваш запрос, используя `{model_fallback}`. "
                "Качество ответа может быть другим.\nПродолжить?"
            ),
        )

    return ResolutionResult(action="proceed")


# ─── Memory injection ───────────────────────────────────────────────────────


def format_memories_for_system_prompt(
    memories: list[dict[str, Any]],
    max_content_length: int = 300,
) -> str:
    """
    Format retrieved memories as an XML block for system_instruction injection.

    Pure function — no I/O.  Returns an empty string if no memories.

    The XML structure is designed so the LLM can cleanly distinguish
    declarative user facts from conversational history (Context Engineering).

    Args:
        memories: List of memory dicts with "content" and optionally "created_at".
        max_content_length: Max chars per memory snippet.

    Returns:
        XML string to append to the system instruction, or "".
    """
    if not memories:
        return ""

    lines: list[str] = ["<long_term_memory>"]
    for m in memories:
        snippet = m["content"][:max_content_length].replace("&", "&amp;").replace("<", "&lt;")
        source = ""
        if "created_at" in m and m["created_at"]:
            source = f' source="{str(m["created_at"])[:10]}"'
        lines.append(f"  <fact{source}>{snippet}</fact>")
    lines.append("</long_term_memory>")
    return "\n".join(lines)


# ─── Response classification ────────────────────────────────────────────────


@dataclass(frozen=True)
class ResponseAction:
    """Outcome of classifying an AI response."""

    action: str  # "error" | "empty" | "send" | "attach_buttons"


def classify_response(
    response_text: str | None,
    was_streamed: bool,
) -> ResponseAction:
    """
    Decide what to do with an AI response.

    Pure function — no I/O.

    Args:
        response_text: The text returned by the AI provider (may be None).
        was_streamed: Whether the response was already displayed via streaming.

    Returns:
        ResponseAction indicating the next step.
    """
    if not response_text:
        return ResponseAction(action="empty")

    # Error patterns (same heuristic as handle_ai_response_error)
    error_indicators = ("500 ", "503 ", "429 ", "error", "unavailable")
    lower = response_text.lower().strip()
    if any(lower.startswith(ind) for ind in error_indicators):
        return ResponseAction(action="error")

    if was_streamed:
        return ResponseAction(action="attach_buttons")

    return ResponseAction(action="send")

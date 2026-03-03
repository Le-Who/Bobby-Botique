# /app/model_selector.py
"""Smart model auto-selection based on message content.

Analyzes user input characteristics to recommend the optimal model:
- Short queries → fast models (e.g. gemini-2.0-flash)
- Complex reasoning → thinking models (e.g. gemini-2.5-pro)
- Image analysis → multimodal models
- Code tasks → code-optimized models
"""

import logging
import re
from dataclasses import dataclass

from app.config import settings


@dataclass
class SelectionResult:
    """Result of model selection analysis."""
    model: str
    reason: str
    confidence: float  # 0.0 - 1.0


# ── Heuristics ───────────────────────────────────────────────────────────────

_CODE_PATTERNS = re.compile(
    r"(```|def |class |function |import |const |let |var |"
    r"программ|код|исправь|баг|debug|refactor|review|"
    r"напиши.*функци|создай.*класс|оптимизир)",
    re.IGNORECASE,
)

_REASONING_PATTERNS = re.compile(
    r"(объясни.*подробно|проанализируй|сравни|"
    r"разбери|почему|как работает|в чём разница|"
    r"pros.*cons|explain.*detail|analyze|compare|"
    r"step.by.step|пошагово|докажи|аргументир)",
    re.IGNORECASE,
)

_SIMPLE_PATTERNS = re.compile(
    r"^(привет|hi|hello|ok|да|нет|ок|спасибо|thanks|"
    r"хорошо|понял|ладно|пока|bye)[\s!?.]*$",
    re.IGNORECASE,
)

_CREATIVE_PATTERNS = re.compile(
    r"(напиши.*стих|сочини|придумай.*историю|"
    r"creative|story|poem|write.*fiction|"
    r"сценарий|рассказ|эссе|essay)",
    re.IGNORECASE,
)


def select_model(
    user_message: str,
    *,
    has_images: bool = False,
    current_model: str | None = None,
) -> SelectionResult | None:
    """Analyze message and suggest an optimal model.

    Returns None if the current model is already suitable (no change needed).
    Only suggests changes when there's a clear mismatch.
    """
    available = set(settings.AVAILABLE_MODELS or [])
    if not available:
        return None

    msg_len = len(user_message)

    # ── Short / trivial messages → fast model ────────────────────────────
    if _SIMPLE_PATTERNS.match(user_message) or msg_len < 20:
        fast_model = _find_model(available, ["flash", "2.0-flash"])
        if fast_model and current_model != fast_model:
            return SelectionResult(
                model=fast_model,
                reason="Короткий запрос — используем быструю модель",
                confidence=0.7,
            )

    # ── Code tasks → best available model ────────────────────────────────
    if _CODE_PATTERNS.search(user_message):
        code_model = _find_model(available, ["2.5-pro", "pro", "2.5-flash"])
        if code_model and current_model != code_model:
            return SelectionResult(
                model=code_model,
                reason="Задача с кодом — используем мощную модель",
                confidence=0.6,
            )

    # ── Deep reasoning → thinking model ──────────────────────────────────
    if _REASONING_PATTERNS.search(user_message) or msg_len > 1000:
        reasoning_model = _find_model(available, ["2.5-pro", "2.5-flash", "pro"])
        if reasoning_model and current_model != reasoning_model:
            return SelectionResult(
                model=reasoning_model,
                reason="Сложный аналитический запрос — используем продвинутую модель",
                confidence=0.5,
            )

    # No strong signal — keep current model
    return None


def _find_model(available: set, preferences: list[str]) -> str | None:
    """Find the first preferred model that's available."""
    for pref in preferences:
        for model in available:
            if pref in model.lower():
                return model
    return None

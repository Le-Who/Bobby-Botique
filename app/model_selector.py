# /app/model_selector.py
"""Smart model auto-selection based on message content.

Analyzes user input characteristics to recommend the optimal model:
- Short queries → fast models (e.g. gemini-3.1-flash-lite-preview)
- Complex reasoning → thinking models (e.g. gemini-2.5-flash)
- Image analysis → multimodal models
- Code tasks → code-optimized models

Design rules:
- NEVER suggest a downgrade (e.g. flash → flash-lite).
- Only suggest upgrades or lateral moves to a better-fit model.
- Suggestions should be non-intrusive hints, not commands.
"""

import re
from dataclasses import dataclass

from app.config import settings


@dataclass
class SelectionResult:
    """Result of model selection analysis."""

    model: str
    reason: str
    confidence: float  # 0.0 - 1.0


# ── Model tier ranking (higher = more capable) ──────────────────────────────

# Order matters: first match wins within _find_model.
# Models are ranked roughly by capability tier.
_MODEL_TIER = {
    # Gemini tiers
    "3-flash-preview": 5,  # flagship
    "3.1-flash-lite": 4,  # excellent performance, better than 2.5
    "2.5-flash": 3,  # standard
    "2.5-flash-lite": 1,  # low latency fallback
    # Opencode Go tiers (relative to each other) — canonical model list only
    "minimax-m2.7": 5,  # flagship
    "kimi-k2.5": 5,  # flagship alternative
    "minimax-m2.5": 4,  # better performance
    "qwen3.6-plus": 4,  # research-grade
    "qwen3.5-plus": 3,  # standard
    "mimo-v2-omni": 2,  # vision specialist
    "big-pickle": 2,  # lightweight
}


def _get_tier(model_name: str) -> int:
    """Get capability tier for a model. Higher = more capable."""
    name = model_name.lower()
    # Check from most specific to least specific
    if "flash-lite" in name:
        if "3.1-flash-lite" in name:
            return 4
        return 1
    if "luma" in name or "dall-e" in name:
        return 1
    if "3-flash-preview" in name:
        return 5
    if "2.5-flash" in name:
        return 3
    return 2  # Unknown models get middle tier


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

# Performance: single shared tuple used by all three _find_model() calls in
# select_model(). Eliminates 3 separate 3-element list allocations per call
# and gives the preference order a single source of truth.
_UPGRADE_PREFERENCE: tuple[str, ...] = ("3-flash-preview", "3.1-flash-lite", "2.5-flash")


def select_model(
    user_message: str,
    *,
    has_images: bool = False,
    current_model: str | None = None,
) -> SelectionResult | None:
    """Analyze message and suggest an optimal model.

    Returns None if the current model is already suitable (no change needed).
    Only suggests UPGRADES — never downgrades to a weaker model.
    """
    # Performance: read the live list directly — _find_model() only iterates, never
    # mutates, so there is no need for a defensive list() copy on every call.
    available = settings.AVAILABLE_MODELS or []
    if not available:
        return None

    # Skip suggestions for OpenRouter / Opencode Go models — different provider,
    # tier logic is Gemini-specific and inapplicable across providers.
    from app.providers import is_opencode_model, is_openrouter_model

    if current_model and (is_openrouter_model(current_model) or is_opencode_model(current_model)):
        return None

    current_tier = _get_tier(current_model) if current_model else 0
    msg_len = len(user_message)

    # ── Code tasks → best available model ────────────────────────────────
    if _CODE_PATTERNS.search(user_message):
        code_model = _find_model(available, _UPGRADE_PREFERENCE)
        if code_model and code_model != current_model and _get_tier(code_model) > current_tier:
            return SelectionResult(
                model=code_model,
                reason="Задача с кодом — используем мощную модель",
                confidence=0.6,
            )

    # ── Deep reasoning → thinking model ──────────────────────────────────
    if _REASONING_PATTERNS.search(user_message) or msg_len > 1000:
        reasoning_model = _find_model(available, _UPGRADE_PREFERENCE)
        if reasoning_model and reasoning_model != current_model and _get_tier(reasoning_model) > current_tier:
            return SelectionResult(
                model=reasoning_model,
                reason="Сложный аналитический запрос — используем продвинутую модель",
                confidence=0.5,
            )

    # ── Creative tasks → flagship model ──────────────────────────────────
    if _CREATIVE_PATTERNS.search(user_message):
        creative_model = _find_model(available, _UPGRADE_PREFERENCE)
        if creative_model and creative_model != current_model and _get_tier(creative_model) > current_tier:
            return SelectionResult(
                model=creative_model,
                reason="Творческая задача — используем продвинутую модель",
                confidence=0.5,
            )

    # ── Short / trivial messages: NO suggestion
    # Rationale: suggesting a downgrade (flash → flash-lite) hurts quality.
    # Suggesting the same tier (flash → flash) is pointless.
    # Short messages are fast on any model — no need to switch.

    # No strong signal — keep current model
    return None


def _find_model(available: list | tuple, preferences: list[str]) -> str | None:
    """Find the first preferred model that's available.

    Uses a list (not set) to preserve ordering.
    Checks preferences in priority order, then available models in config order.
    """
    for pref in preferences:
        for model in available:
            if pref in model.lower():
                return model
    return None

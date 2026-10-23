# /app/model_selector.py
"""Smart model auto-selection based on message content.

Analyzes user input characteristics to recommend the optimal model:
- Short queries → fast models (e.g. gemini-3.1-flash-lite)
- Complex reasoning → primary capable models (e.g. gemini-3.5-flash)
- Image analysis → multimodal models
- Code tasks → code-optimized models

Design rules:
- NEVER suggest a downgrade (e.g. flash → flash-lite).
- Only suggest upgrades or lateral moves to a better-fit model.
- Suggestions should be non-intrusive hints, not commands.
"""

import re
from dataclasses import dataclass

from app.config import CURRENT_GEMINI_MODELS, DEFAULT_GEMINI_MODELS, settings


@dataclass
class SelectionResult:
    """Result of model selection analysis."""

    model: str
    reason: str
    confidence: float  # 0.0 - 1.0


# ── Model tier ranking (higher = more capable) ──────────────────────────────

# Known application model families only; unknown IDs cannot justify an upgrade.
_MODEL_TIER = {model: 4 if "flash-lite" in model else 5 for model in CURRENT_GEMINI_MODELS}


def _get_tier(model_name: str) -> int:
    """Get capability tier for a model. Higher = more capable."""
    name = model_name.lower()
    if name in _MODEL_TIER:
        return _MODEL_TIER[name]
    if "flash-lite" in name:
        if "3.1-flash-lite" in name:
            return 4
        return 1
    if "luma" in name or "dall-e" in name:
        return 1
    if "3.5-flash" in name:
        return 5
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
_UPGRADE_PREFERENCE: tuple[str, ...] = CURRENT_GEMINI_MODELS


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
    configured = getattr(settings, "AVAILABLE_MODELS", None) if settings is not None else None
    available = configured if isinstance(configured, list) else DEFAULT_GEMINI_MODELS
    if not available:
        return None

    # Skip suggestions for OpenRouter / Opencode Go / FreeTheAI models — different provider,
    # tier logic is Gemini-specific and inapplicable across providers.
    from app.providers import is_freetheai_model, is_opencode_model, is_openrouter_model

    if current_model and (
        is_openrouter_model(current_model) or is_opencode_model(current_model) or is_freetheai_model(current_model)
    ):
        return None
    if current_model and current_model.lower() not in _MODEL_TIER:
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


def _find_model(available: list | tuple, preferences: list[str] | tuple[str, ...]) -> str | None:
    """Find the first preferred model that's available.

    Uses a list (not set) to preserve ordering.
    Checks preferences in priority order, then available models in config order.
    """
    for pref in preferences:
        preferred_name = pref.lower()
        if not preferred_name.startswith("gemini-"):
            preferred_name = f"gemini-{preferred_name}"
        for model in available:
            if model.lower() == preferred_name:
                return model
    return None

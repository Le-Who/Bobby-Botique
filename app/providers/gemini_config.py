"""Model-aware configuration for Gemini text generation, including direct callers."""

from typing import Any

from google.genai import types

from app.providers.base import _build_thinking_config


def _normalize_thinking_config(model: str, value: Any) -> types.ThinkingConfig | None:
    config = types.ThinkingConfig.model_validate(value)
    if config.thinking_level is not None and config.thinking_budget is not None:
        raise ValueError("Choose thinking_level or thinking_budget, not both")
    if _build_thinking_config(model, "low") is None:
        return None
    if config.thinking_level is not None:
        level = config.thinking_level.value.lower()
        if level == "xhigh":
            if not model.startswith("gemini-4-hydrogen"):
                raise ValueError("This model does not support xhigh thinking")
            return config
        level = "off" if level == "minimal" else level
    elif config.thinking_budget is not None:
        budget = config.thinking_budget
        if model.startswith("gemini-2.5-"):
            pro = model.startswith("gemini-2.5-pro")
            minimum = 128 if pro else 512 if model.startswith("gemini-2.5-flash-lite") else 1
            maximum = 32768 if pro else 24576
            if budget < -1:
                raise ValueError("Invalid thinking budget")
            if budget > 0 or (budget == 0 and pro):
                budget = min(maximum, max(minimum, budget))
            return config.model_copy(update={"thinking_budget": budget})
        if budget == -1:
            return config.model_copy(update={"thinking_budget": None})
        level = "off" if budget == 0 else "low" if budget <= 1024 else "medium" if budget <= 8192 else "high"
    else:
        return config
    supported = _build_thinking_config(model, level)
    if supported is None:
        return None
    return config.model_copy(
        update={"thinking_level": supported.thinking_level, "thinking_budget": supported.thinking_budget}
    )


def build_generate_content_config(model_name: str, **options: Any) -> types.GenerateContentConfig:
    """Normalize Gemini text settings for the model actually receiving the call.

    Google recommends default sampling for Gemini 3/4 text models; since
    3.5 Flash-Lite/3.6 Flash those fields are deprecated and ignored. Speech,
    Live and image generation have separate contracts and must not use this
    text-only builder. Direct callers share the same thinking constraints as chat.
    """
    model = model_name.rsplit("/", 1)[-1].lower()
    if model.startswith(("gemini-3", "gemini-4")):
        for field in ("temperature", "top_p", "top_k", "candidate_count", "presence_penalty", "frequency_penalty"):
            options.pop(field, None)
    if options.get("thinking_config") is not None:
        options["thinking_config"] = _normalize_thinking_config(model, options["thinking_config"])
    limit = options.get("max_output_tokens")
    if (
        model.startswith("gemini-3.8-")
        and isinstance(limit, int)
        and limit < 1024
        and not options.get("thinking_config")
    ):
        supported = _build_thinking_config(model, "low")
        if supported is not None:
            options["thinking_config"] = supported
            options["max_output_tokens"] = 1024
    return types.GenerateContentConfig(**options)

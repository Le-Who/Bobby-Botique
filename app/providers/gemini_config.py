"""Model-aware configuration for Gemini text generation, including direct callers."""

from typing import Any

from google.genai import types


def build_generate_content_config(model_name: str, **options: Any) -> types.GenerateContentConfig:
    """Keep Gemini 3 text sampling at provider defaults without changing legacy models.

    Google recommends default sampling for all Gemini 3 text models; since
    3.5 Flash-Lite/3.6 Flash those fields are deprecated and ignored. Speech,
    Live and image generation have separate contracts and must not use this
    text-only builder.
    """
    model = model_name.rsplit("/", 1)[-1].lower()
    if model.startswith("gemini-3"):
        for field in ("temperature", "top_p", "top_k", "candidate_count"):
            options.pop(field, None)
    # Tiny legacy caps count thought tokens too. On 3.8 its default medium
    # reasoning can spend the whole allowance before emitting a classifier
    # label. Keep explicit policies intact; reserve room for bounded callers.
    limit = options.get("max_output_tokens")
    if (
        model.startswith("gemini-3.8-")
        and isinstance(limit, int)
        and limit < 1024
        and not options.get("thinking_config")
    ):
        options["thinking_config"] = types.ThinkingConfig(thinking_level="low")  # type: ignore[arg-type]
        options["max_output_tokens"] = 1024
    return types.GenerateContentConfig(**options)

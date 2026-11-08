"""Conversion of persisted/handler history into the typed provider boundary."""

from __future__ import annotations

from typing import Any

from PIL import Image

from app.providers.base import is_openai_model
from app.providers.stream_types import (
    GenerationRequest,
    GroundingMode,
    ImagePart,
    PromptRole,
    PromptTurn,
    RequestScope,
    ServiceTier,
    TextPart,
    ThinkingLevel,
    Workload,
)
from app.utils.image_utils import TaggedImage, save_image_as_bytes


def _image_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _prompt_role(value: Any) -> PromptRole:
    normalized = str(value or "user").lower()
    if normalized in {"model", "assistant"}:
        return PromptRole.MODEL
    if normalized == "user":
        return PromptRole.USER
    raise ValueError(f"Unsupported prompt role: {value!r}")


async def _prompt_part(value: Any):
    if isinstance(value, str):
        return TextPart(value) if value.strip() else None
    if isinstance(value, TaggedImage):
        return ImagePart(
            data=value.data,
            mime_type=_image_mime_type(value.data),
            needs_compression=not value.pre_compressed,
            cache_key=value.cache_key,
            task_type=value.task_type,
        )
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        if not value:
            return None
        return ImagePart(
            data=value,
            mime_type=_image_mime_type(value),
            needs_compression=True,
        )
    if isinstance(value, Image.Image):
        encoded = await save_image_as_bytes(value)
        if not encoded:
            raise ValueError("Could not encode image prompt part")
        return ImagePart(
            data=encoded,
            mime_type=_image_mime_type(encoded),
            needs_compression=False,
        )
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        text = value["text"]
        return TextPart(text) if text.strip() else None
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.strip():
        return TextPart(text)
    raise TypeError(f"Unsupported prompt part type: {type(value).__name__}")


async def generation_request_from_history(
    *,
    models: tuple[str, ...] | list[str],
    history: list[dict[str, Any]],
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    privacy_epoch: int | None = None,
    operation_id: str | None = None,
    thinking_level: str | ThinkingLevel | None = None,
    grounding: GroundingMode = GroundingMode.NONE,
    workload: Workload = Workload.INTERACTIVE,
    allow_deferred: bool = True,
    allow_key_race: bool = True,
    process_id: str | None = None,
    service_tier: ServiceTier = ServiceTier.DEFAULT,
) -> GenerationRequest:
    """Convert handler history and legacy/user preferences before provider routing.

    Direct OpenAI uses model defaults unless a resolved process supplies an
    administrator reasoning override. Internal typed requests retain their own
    explicit reasoning and do not use this user-preference conversion.
    """
    turns: list[PromptTurn] = []
    for entry in history:
        if not isinstance(entry, dict):
            raise TypeError("History entries must be mappings")
        raw_parts = entry.get("parts", ())
        if not isinstance(raw_parts, (list, tuple)):
            raw_parts = (raw_parts,)
        parts = []
        for raw_part in raw_parts:
            part = await _prompt_part(raw_part)
            if part is not None:
                parts.append(part)
        if parts:
            turns.append(PromptTurn(role=_prompt_role(entry.get("role")), parts=tuple(parts)))

    if thinking_level is None or isinstance(thinking_level, ThinkingLevel):
        typed_thinking = thinking_level
    else:
        typed_thinking = ThinkingLevel(thinking_level.lower())

    route_strategy = "legacy"
    explicit = False
    revision = 0
    admin_reasoning = False
    if process_id is not None:
        from app.process_policies import policy_thinking_level, resolve_process

        policy = await resolve_process(process_id, tuple(models))
        models = policy.models
        route_strategy, explicit, revision = policy.strategy, policy.explicit, policy.revision
        service_tier = ServiceTier(getattr(policy, "service_tier", ServiceTier.DEFAULT))
        admin_reasoning = getattr(policy, "reasoning_effort", None) is not None
        if not admin_reasoning and all(is_openai_model(model) for model in models):
            typed_thinking = None
        typed_thinking = policy_thinking_level(policy, typed_thinking)
    elif all(is_openai_model(model) for model in models):
        typed_thinking = None

    return GenerationRequest(
        models=tuple(models),
        turns=tuple(turns),
        system_instruction=system_instruction,
        scope=RequestScope(user_id=user_id, chat_id=chat_id, privacy_epoch=privacy_epoch, operation_id=operation_id),
        thinking_level=typed_thinking,
        grounding=grounding,
        workload=workload,
        # Only an epoch captured by the original operation can authorize later
        # private retention. Never resolve a new epoch after history assembly.
        allow_deferred=allow_deferred and privacy_epoch is not None and bool(operation_id),
        allow_key_race=allow_key_race,
        route_strategy=route_strategy,
        allow_model_fallback=not explicit,
        policy_revision=revision,
        service_tier=service_tier,
        openai_uses_model_default=not admin_reasoning,
    )


def deferred_history_from_request(request: GenerationRequest) -> list[dict[str, Any]]:
    """Serialize only text turns for the size-bounded deferred queue."""
    history: list[dict[str, Any]] = []
    for turn in request.turns:
        text_parts = [part.text for part in turn.parts if isinstance(part, TextPart)]
        if text_parts:
            history.append({"role": turn.role.value, "parts": text_parts})
    return history


__all__ = ["deferred_history_from_request", "generation_request_from_history"]

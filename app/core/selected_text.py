"""Bounded text collection for live consumers with an explicit provider plan."""

import asyncio
from contextlib import aclosing

from pydantic import BaseModel

from app.process_policies import ResolvedPolicy
from app.providers.stream_types import (
    GenerationRequest,
    PromptRole,
    PromptTurn,
    RequestScope,
    StreamCompleted,
    TextDelta,
    TextPart,
    ThinkingLevel,
)


async def generate_selected_text(
    policy: ResolvedPolicy,
    prompt: str,
    *,
    timeout: float,
    system_instruction: str | None = None,
    user_id: int | None = None,
    privacy_epoch: int | None = None,
    response_schema: type[BaseModel] | None = None,
) -> str:
    """Run exactly the supplied models; cancellation closes the owned provider stream."""
    from app.providers import get_provider_router

    request = GenerationRequest(
        models=policy.models,
        turns=(PromptTurn(PromptRole.USER, (TextPart(prompt),)),),
        system_instruction=system_instruction,
        scope=RequestScope(user_id=user_id, privacy_epoch=privacy_epoch),
        thinking_level=ThinkingLevel.OFF,
        allow_deferred=False,
        route_strategy=policy.strategy if policy.explicit else "sequential",
        allow_model_fallback=False,
        allow_key_race=False,
        policy_revision=policy.revision,
        response_schema=response_schema,
    )
    chunks: list[str] = []
    async with asyncio.timeout(timeout):
        async with aclosing(get_provider_router().stream(request)) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                elif isinstance(event, StreamCompleted):
                    text = "".join(chunks).strip()
                    if text:
                        return text
                    raise ValueError("Empty configured text response")
                else:
                    raise RuntimeError("Configured text route did not complete")
    raise RuntimeError("Configured text stream ended without completion")

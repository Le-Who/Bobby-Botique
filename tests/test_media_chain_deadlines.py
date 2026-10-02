"""Media fallback chains share a deadline and select keys for the actual model."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.process_policies import ResolvedPolicy
from app.utils import multimodal_processor as media


async def invoke(kind):
    if kind == "asr":
        return await media._transcribe_gemini(
            b"ogg", "legacy-key", mime_type="audio/ogg", model=media.TRANSCRIPTION_MODEL
        )
    return await media._classify_intent_with_fallback([types.Part.from_text(text="weather")], "legacy-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["asr", "intent"])
async def test_explicit_chain_reselects_keys_for_each_actual_model(monkeypatch, kind):
    monkeypatch.setattr(
        media, "resolve_process", AsyncMock(return_value=ResolvedPolicy(("gemini-new",), "sequential", True, 2))
    )
    generate = AsyncMock(return_value="spoken\nINTENT:SEARCH")
    monkeypatch.setattr(media, "_generate_with_resilience", generate)
    await invoke(kind)
    assert generate.await_args.kwargs["model"] == "gemini-new"
    assert generate.await_args.kwargs["api_key"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["asr", "intent"])
async def test_explicit_chain_does_not_restart_budget_for_reserves(monkeypatch, kind):
    monkeypatch.setattr(
        media,
        "resolve_process",
        AsyncMock(
            return_value=ResolvedPolicy(("gemini-first", "gemini-reserve", "gemini-third"), "sequential", True, 2)
        ),
    )
    monkeypatch.setattr(media, "_MEDIA_PLAN_TIMEOUT", 0.03, raising=False)
    attempted = []
    cancelled = asyncio.Event()

    async def generate(*, model, **_):
        attempted.append(model)
        if model == "gemini-first":
            await asyncio.sleep(0.01)
            return None
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(media, "_generate_with_resilience", generate)
    # The outer guard only protects the regression test from an unbounded path.
    result = await asyncio.wait_for(invoke(kind), 0.3)
    assert result == ((None, "conversational", None) if kind == "asr" else None)
    assert attempted == ["gemini-first", "gemini-reserve"]
    assert cancelled.is_set()

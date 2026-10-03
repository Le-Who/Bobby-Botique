"""Media fallback chains share a deadline and select keys for the actual model."""

import asyncio
from types import SimpleNamespace
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
    if kind == "asr":
        audio = generate.await_args.kwargs["parts"][0].inline_data
        assert (audio.data, audio.mime_type) == (b"ogg", "audio/ogg")


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
    monkeypatch.setattr(media, "_MEDIA_PLAN_TIMEOUT", 5)
    attempted = []
    cancelled = asyncio.Event()
    reserve_started = asyncio.Event()
    timeouts = []
    observed_deadlines = []

    def observe_timeout(delay):
        timeout = asyncio.timeout(delay)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(media, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": observe_timeout}))

    async def generate(*, model, **_):
        attempted.append(model)
        observed_deadlines.append(timeouts[-1].when())
        if model == "gemini-first":
            return None
        try:
            reserve_started.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(media, "_generate_with_resilience", generate)
    task = asyncio.create_task(invoke(kind))
    try:
        await asyncio.wait_for(reserve_started.wait(), 1)
        assert len(timeouts) == 1
        assert observed_deadlines[0] == observed_deadlines[1]
        # Expire the real timeout after entry into the reserve, without a sleep.
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert result == ((None, "conversational", None) if kind == "asr" else None)
    assert attempted == ["gemini-first", "gemini-reserve"]
    assert cancelled.is_set()

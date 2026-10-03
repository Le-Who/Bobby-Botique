"""Provider plans preserve their SDK/HTTP contracts and exact fallback order."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.process_policies import ResolvedPolicy, validate_policy
from app.runtime_settings import result_execution


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "process,model",
    [
        ("image.pollinations", "qwen/qwen-image-3"),
        ("image.fta", "vhr/future-image"),
        ("music.fta", "or/google/lyria-next"),
        ("asr.pollinations", "whisper-next"),
        ("tts.elevenlabs", "eleven_next"),
        ("live.vertex", "gemini-next-live"),
    ],
)
async def test_specialized_endpoint_accepts_new_compatible_model_id(process, model):
    assert validate_policy(process, {"models": [model]})["models"] == [model]


@pytest.mark.asyncio
async def test_fta_images_keep_edit_parameters_and_honor_new_explicit_ids(monkeypatch):
    from app.providers.freetheai_image import FreeTheAIImageProvider, FTAImageResult

    monkeypatch.setattr(
        result_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("vhr/future-image", "img/reserve-image"), "sequential", True, 7)),
    )
    execute = AsyncMock(
        side_effect=[FTAImageResult(False, error_message="overloaded"), FTAImageResult(True, images=[b"png"])]
    )
    provider = FreeTheAIImageProvider()
    monkeypatch.setattr(provider, "_generate_one_model", execute)
    result = await provider.generate("cat", size="1024x1024", quality="hd", image_base64="input")
    assert result.images == [b"png"]
    assert [call.args[1] for call in execute.await_args_list] == ["vhr/future-image", "img/reserve-image"]
    assert all(
        call.kwargs == {"size": "1024x1024", "quality": "hd", "image_base64": "input", "_honor_model": True}
        for call in execute.await_args_list
    )


@pytest.mark.asyncio
async def test_elevenlabs_restarts_whole_message_with_next_model(monkeypatch):
    from app.providers import elevenlabs_tts

    monkeypatch.setattr(
        result_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("eleven_first", "eleven_second"), "sequential", True, 7)),
    )
    execute = AsyncMock(side_effect=[None, [b"first", b"second"]])
    monkeypatch.setattr(elevenlabs_tts, "_generate_message_with_key_rotation", execute)
    assert await elevenlabs_tts.generate_speech_with_key_rotation(["one", "two"], ["test-key"], voice_id="voice") == [
        b"first",
        b"second",
    ]
    assert [call.kwargs["model_id"] for call in execute.await_args_list] == ["eleven_first", "eleven_second"]
    assert all(call.args[0] == ["one", "two"] for call in execute.await_args_list)


@pytest.mark.asyncio
async def test_terminal_image_failure_does_not_try_paid_reserve(monkeypatch):
    from app.providers.pollinations import PollinationsProvider, PollinationsResult

    monkeypatch.setattr(
        result_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("flux", "reserve"), "sequential", True, 7)),
    )
    provider = PollinationsProvider()
    execute = AsyncMock(return_value=PollinationsResult(False, error_message="unauthorized"))
    monkeypatch.setattr(provider, "_generate_one_model", execute)
    assert not (await provider.generate("cat")).success
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_total_deadline_returns_provider_failure_and_cancels_attempt(monkeypatch):
    monkeypatch.setattr(
        result_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("primary", "reserve"), "sequential", True, 7)),
    )
    stopped = asyncio.Event()
    calls = []

    async def execute(candidate, explicit):
        calls.append(candidate)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    result = await result_execution.run_result_process(
        "image.fta", "baseline", execute, success=bool, timeout=0.01, on_timeout=lambda: "timeout"
    )
    assert result == "timeout"
    assert stopped.is_set()
    assert calls == ["primary"]


@pytest.mark.asyncio
async def test_external_cancellation_is_not_converted_to_timeout(monkeypatch):
    monkeypatch.setattr(
        result_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("primary",), "sequential", True, 7)),
    )
    started = asyncio.Event()

    async def execute(candidate, explicit):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        result_execution.run_result_process(
            "image.fta", "baseline", execute, success=bool, timeout=1, on_timeout=lambda: "timeout"
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_gemini_image_chain_has_one_deadline(monkeypatch):
    from app.providers import imagen_provider as images

    monkeypatch.setattr(
        images,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-first-image", "gemini-second-image"), "sequential", True, 1)),
    )
    monkeypatch.setattr(images.settings, "IMAGE_GEN_TIMEOUT", 0.01)
    attempted = []
    stopped = asyncio.Event()

    async def execute(prompt, model, *args, **kwargs):
        attempted.append(model)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    provider = images.ImagenProvider()
    monkeypatch.setattr(provider, "_generate_one_model", execute)
    assert (await provider.generate("cat")).error_message == "timeout"
    assert attempted == ["gemini-first-image"]
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_crocodile_deadline_does_not_restart_for_reserve(monkeypatch):
    from app.games import daily_ai

    monkeypatch.setattr(
        daily_ai,
        "resolve_process",
        AsyncMock(
            return_value=ResolvedPolicy(("gemini-first", "gemini-second", "gemini-third"), "sequential", True, 1)
        ),
    )
    attempted = []
    reserve_started = asyncio.Event()
    cleaned = asyncio.Event()
    timeouts = []
    observed_deadlines = []

    def observe_timeout(delay):
        timeout = asyncio.timeout(delay)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(daily_ai, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": observe_timeout}))

    async def generate(prompt, model, timeout):
        attempted.append(model)
        observed_deadlines.append(timeouts[-1].when())
        if model == "gemini-first":
            return "invalid JSON"
        reserve_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    task = asyncio.create_task(daily_ai.generate_daily_text_for("words", "prompt", "gemini-baseline", timeout=5))
    try:
        await asyncio.wait_for(reserve_started.wait(), 1)
        assert len(timeouts) == 1
        assert observed_deadlines[0] == observed_deadlines[1]
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert attempted == ["gemini-first", "gemini-second"]
    assert cleaned.is_set()

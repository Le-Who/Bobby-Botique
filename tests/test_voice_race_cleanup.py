"""TTS key races own and await every SDK task on success and cancellation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_tts_race_awaits_losing_sdk_cleanup(monkeypatch, cancel_parent):
    from app import voice_engine

    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(
            side_effect=[
                {"api_key": "first", "key_hash": "first-hash"},
                {"api_key": "second", "key_hash": "second-hash"},
            ]
        ),
    )
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def generate(text, key, **kwargs):
        if key == "first" and not cancel_parent:
            await started.wait()
            return b"pcm"
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            # Cleanup completes asynchronously; merely calling cancel is insufficient.
            await asyncio.sleep(0)
            stopped.set()

    monkeypatch.setattr("app.providers.tts.generate_speech", generate)
    task = asyncio.create_task(voice_engine._generate_single_chunk_gemini("hello", "Aoede", set()))
    await started.wait()
    if cancel_parent:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == b"pcm"
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_explicit_tts_plan_rejects_partial_message_for_reserve(monkeypatch):
    from app import voice_engine

    monkeypatch.setattr(voice_engine, "_generate_single_chunk_gemini", AsyncMock(side_effect=[b"pcm", None]))
    monkeypatch.setattr("app.utils.audio.trim_trailing_silence", lambda value: value)
    assert await voice_engine._run_gemini_pipeline(["one", "two"], "Aoede", 1, require_complete=True) is None


@pytest.mark.asyncio
async def test_gemini_tts_refuses_sdk_without_quota_admission(monkeypatch):
    from app.providers import tts

    generate = AsyncMock()
    monkeypatch.setattr(
        tts,
        "get_cached_genai_client",
        lambda _: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
    )
    reserved = AsyncMock(return_value=False)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserved)
    with pytest.raises(tts.TTSAdmissionRejected):
        await tts.generate_speech("hello", "test-key", model_name="gemini-test-tts")
    reserved.assert_awaited_once()
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_tts_models_share_whole_message_deadline(monkeypatch):
    from types import MappingProxyType
    from unittest.mock import MagicMock

    from app import voice_engine
    from app.process_policies import ResolvedPolicy
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.runtime_settings.store import SettingsSnapshot

    job = voice_engine.VoiceJob("job", 1, 1, 1, "hello", "Aoede", None, "source", MagicMock(), 0)

    async def resolve(process_id, baseline):
        models = (
            ("gemini",)
            if process_id == "tts.delivery"
            else ("gemini-first-tts", "gemini-reserve-tts", "gemini-third-tts")
        )
        return ResolvedPolicy(models, "sequential", True, 1)

    attempted = []
    cleaned = asyncio.Event()
    reserve_started = asyncio.Event()
    timeouts = []
    observed_deadlines = []

    def observe_timeout(delay):
        timeout = asyncio.timeout(delay)
        timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(voice_engine, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": observe_timeout}))

    async def synthesize(*_, model_name, **__):
        attempted.append(model_name)
        observed_deadlines.append(timeouts[-1].when())
        if len(attempted) == 1:
            return None
        try:
            reserve_started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", synthesize)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        task = asyncio.create_task(voice_engine.VoiceReplyManager()._pregenerate_audio(job))
        try:
            await asyncio.wait_for(reserve_started.wait(), 1)
            assert len(timeouts) == 1
            assert observed_deadlines[0] == observed_deadlines[1]
            timeouts[0].reschedule(asyncio.get_running_loop().time())
            assert await asyncio.wait_for(task, 1) is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert attempted == ["gemini-first-tts", "gemini-reserve-tts"]
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_gemini_deadline_still_allows_next_delivery_provider(monkeypatch):
    from types import MappingProxyType
    from unittest.mock import MagicMock

    from app import voice_engine
    from app.config import settings
    from app.process_policies import ResolvedPolicy
    from app.runtime_settings.lifecycle import runtime_settings_scope
    from app.runtime_settings.store import SettingsSnapshot
    from app.utils import audio

    async def resolve(process_id, baseline):
        models = ("gemini", "elevenlabs") if process_id == "tts.delivery" else ("gemini-test-tts",)
        return ResolvedPolicy(models, "sequential", True, 1)

    async def hang(*_, **__):
        await asyncio.Event().wait()

    job = voice_engine.VoiceJob("job", 1, 1, 1, "hello", "Aoede", None, "source", MagicMock(), 0)
    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr(voice_engine, "_GEMINI_PLAN_TIMEOUT_FACTOR", 0.0005)
    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", hang)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["test-key"])
    reserve = AsyncMock(return_value=[b"pcm"])
    monkeypatch.setattr("app.providers.elevenlabs_tts.generate_speech_with_key_rotation", reserve)
    monkeypatch.setattr(audio, "crossfade_pcm_chunks", lambda parts: b"pcm")
    monkeypatch.setattr(audio, "pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        assert await voice_engine.VoiceReplyManager()._pregenerate_audio(job) == b"ogg"
    reserve.assert_awaited_once()

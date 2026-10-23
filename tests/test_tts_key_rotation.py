"""Whole-message TTS must not duplicate synthesis or strand usable keys."""

import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import voice_engine
from app.process_policies import ResolvedPolicy
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.runtime_settings.store import SettingsSnapshot


@pytest.mark.asyncio
async def test_long_gemini_reply_is_one_unmodified_synthesis(monkeypatch):
    text = "Это достаточно длинное предложение для проверки цельной озвучки. " * 100
    seen = []

    async def synthesize(transcript, key, **kwargs):
        seen.append((transcript, key))
        return b"\x01\x00" * 240

    async def key(*args, excluded_hashes, **kwargs):
        for value in ("first", "second"):
            if value not in excluded_hashes:
                return {"key_hash": value, "api_key": value}
        return None

    async def resolve(process_id, baseline):
        return ResolvedPolicy(("gemini",)) if process_id == "tts.delivery" else ResolvedPolicy(baseline)

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(manager, "_set_status", AsyncMock())
    job = SimpleNamespace(response_text=text.strip(), voice="Aoede", tts_temperature=None)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        assert await manager._pregenerate_audio(job) == b"ogg"
    assert seen == [(text.strip(), "first")]


@pytest.mark.asyncio
async def test_local_quota_rejection_rotates_across_entire_pool_without_suspension(monkeypatch):
    from app.providers.tts import TTSAdmissionRejected

    requests = []
    suspend = AsyncMock()

    async def key(*args, excluded_hashes, **kwargs):
        for value in ("a", "b", "c", "d", "e", "f"):
            if value not in excluded_hashes:
                return {"key_hash": value, "api_key": value}
        return None

    async def synthesize(text, key, **kwargs):
        requests.append(key)
        if key != "f":
            raise TTSAdmissionRejected("local quota exhausted")
        return b"pcm"

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    assert await voice_engine._generate_single_chunk_gemini("whole text", "Aoede", set()) == b"pcm"
    assert requests == ["a", "b", "c", "d", "e", "f"]
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_cancellation_finishes_sdk_cleanup_and_releases_request_slot(monkeypatch):
    entered, cleaned = asyncio.Event(), asyncio.Event()
    semaphore = asyncio.Semaphore(1)

    async def synthesize(*args, **kwargs):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key", AsyncMock(return_value={"key_hash": "a", "api_key": "a"})
    )
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    task = asyncio.create_task(
        voice_engine._generate_single_chunk_gemini("whole text", "Aoede", set(), request_semaphore=semaphore)
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    await asyncio.wait_for(semaphore.acquire(), 1)
    semaphore.release()


@pytest.mark.asyncio
async def test_elevenlabs_reply_below_model_limit_is_one_request(monkeypatch):
    from app.config import settings

    text = "Длинное предложение для озвучивания. " * 180
    seen = []

    async def synthesize(transcript, key, **kwargs):
        seen.append(transcript)
        return b"\x01\x00" * 240

    async def resolve(process_id, baseline):
        return ResolvedPolicy(("elevenlabs",)) if process_id == "tts.delivery" else ResolvedPolicy(baseline)

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEYS", ["fake-key"])
    monkeypatch.setattr("app.providers.elevenlabs_tts.generate_speech_elevenlabs", synthesize)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(manager, "_set_status", AsyncMock())
    job = SimpleNamespace(response_text=text.strip(), voice="", tts_temperature=None)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        assert await manager._pregenerate_audio(job) == b"ogg"
    assert seen == [text.strip()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Quota exceeded: generate_requests_per_minute", "rate_limit"),
        ("Quota exceeded: GenerateRequestsPerDay", "quota"),
        ("RESOURCE_EXHAUSTED", "rate_limit"),
    ],
)
async def test_provider_429_rotates_and_distinguishes_minute_from_daily_quota(monkeypatch, message, category):
    from google.genai.errors import ClientError

    seen, penalties = [], []

    async def key(*args, excluded_hashes, **kwargs):
        return {"key_hash": "b", "api_key": "b"} if "a" in excluded_hashes else {"key_hash": "a", "api_key": "a"}

    async def synthesize(text, key, **kwargs):
        seen.append(key)
        if key == "a":
            raise ClientError(429, {"error": {"message": message, "status": "RESOURCE_EXHAUSTED"}})
        return b"pcm"

    async def suspend(key, model, penalty, details):
        penalties.append((key, penalty))

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    assert await voice_engine._generate_single_chunk_gemini("whole text", "Aoede", set()) == b"pcm"
    assert seen == ["a", "b"]
    assert penalties == [("a", category)]


@pytest.mark.asyncio
async def test_all_minute_full_keys_wait_then_retry_without_key_penalty(monkeypatch):
    from app.providers.tts import TTSAdmissionRejected

    attempts, sleeps = [], []
    clock = [100.0]
    suspend = AsyncMock()

    async def key(*args, excluded_hashes, **kwargs):
        for value in ("a", "b"):
            if value not in excluded_hashes:
                return {"key_hash": value, "api_key": value}
        return None

    async def synthesize(text, key, **kwargs):
        attempts.append(key)
        if len(attempts) <= 2:
            raise TTSAdmissionRejected("RPM full", retry_after=1.0)
        return b"pcm"

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(voice_engine, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(voice_engine, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    assert await voice_engine._generate_single_chunk_gemini("whole text", "Aoede", set()) == b"pcm"
    assert attempts == ["a", "b", "a"]
    assert sleeps == [1.0]
    suspend.assert_not_awaited()


@pytest.mark.asyncio
async def test_elevenlabs_hard_limit_keeps_full_text_and_budgets_each_required_request(monkeypatch):
    from app.providers import elevenlabs_tts
    from app.runtime_settings import result_execution

    transcript = "а" * 10001
    seen, deadlines = [], []

    async def resolve(*args):
        return ResolvedPolicy(("eleven_multilingual_v2",), "sequential", True, 1)

    async def synthesize(text, key, **kwargs):
        seen.append(text)
        return b"\x01\x00" * 240

    def observe_timeout(delay):
        deadlines.append(delay)
        return asyncio.timeout(delay)

    monkeypatch.setattr("app.process_policies.resolve_process", resolve)
    monkeypatch.setattr(result_execution, "resolve_process", resolve)
    monkeypatch.setattr(result_execution, "asyncio", SimpleNamespace(timeout=observe_timeout))
    monkeypatch.setattr(elevenlabs_tts, "generate_speech_elevenlabs", synthesize)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        result = await elevenlabs_tts.generate_speech_with_key_rotation(
            [transcript], ["key"], voice_id="voice", timeout=1
        )
    assert result is not None and len(result) == 2
    assert list(map(len, seen)) == [10000, 1]
    assert "".join(seen) == transcript
    assert deadlines == [2]

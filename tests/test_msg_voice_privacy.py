"""Durable privacy boundaries for voice-message ingress."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def voice_input(monkeypatch):
    state = SimpleNamespace(memory_epoch=52, _has_persisted_chat=True)
    generation = AsyncMock(return_value=52)
    download = AsyncMock(return_value=b"ogg")
    asr = AsyncMock(return_value=(None, "conversational", None))
    monkeypatch.setattr("app.handlers.msg_voice.get_user_chat", AsyncMock(return_value=state))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", generation)
    monkeypatch.setattr("app.handlers.msg_voice.get_file_bytes", download)
    monkeypatch.setattr("app.utils.multimodal_processor.transcribe_voice", asr)
    placeholder = MagicMock(edit_text=AsyncMock())
    voice_file = object()
    voice = SimpleNamespace(get_file=AsyncMock(return_value=voice_file))
    context = SimpleNamespace(bot=object())
    return SimpleNamespace(
        placeholder=placeholder,
        update=MagicMock(),
        context=context,
        voice=voice,
        voice_file=voice_file,
        generation=generation,
        download=download,
        asr=asr,
    )


async def process_voice(input):
    from app.handlers.msg_voice import _process_voice_pipeline

    await _process_voice_pipeline(input.placeholder, input.update, input.context, 123, input.voice, "ru")


@pytest.mark.asyncio
async def test_voice_asr_runs_inside_exact_generation_lease(monkeypatch, voice_input):
    active = False
    observed = []

    @asynccontextmanager
    async def tracked_lease(user_id, expected_epoch, *, purpose, require_ltm):
        nonlocal active
        assert user_id == 123
        assert expected_epoch == 52
        assert purpose == "conversation:voice-ingress"
        assert require_ltm is False
        active = True
        observed.append("enter")
        try:
            yield True
        finally:
            active = False
            observed.append("exit")

    async def transcribe(audio_bytes, *, mime_type):
        assert active
        assert (audio_bytes, mime_type) == (b"ogg", "audio/ogg")
        observed.append("asr")
        return None, "conversational", None

    voice_input.asr.side_effect = transcribe
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", tracked_lease)
    await process_voice(voice_input)
    assert observed == ["enter", "asr", "exit"]
    assert active is False
    voice_input.generation.assert_awaited_once_with(123, expected_epoch=52)
    voice_input.download.assert_awaited_once_with(voice_input.context.bot, voice_input.voice_file)
    voice_input.asr.assert_awaited_once_with(b"ogg", mime_type="audio/ogg")


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["generation", "lease"])
async def test_stale_voice_generation_never_downloads_or_transcribes(monkeypatch, voice_input, denial):
    entered = []

    @asynccontextmanager
    async def denied_lease(user_id, epoch, **kwargs):
        entered.append((user_id, epoch, kwargs))
        yield False

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", denied_lease)
    if denial == "generation":
        voice_input.generation.return_value = None
    await process_voice(voice_input)
    assert len(entered) == (denial == "lease")
    if entered:
        assert entered[0] == (123, 52, {"purpose": "conversation:voice-ingress", "require_ltm": False})
    voice_input.voice.get_file.assert_not_awaited()
    voice_input.download.assert_not_awaited()
    voice_input.asr.assert_not_awaited()
    voice_input.placeholder.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_asr_releases_voice_lease(monkeypatch, voice_input):
    started = asyncio.Event()
    events = []

    @asynccontextmanager
    async def tracked_lease(*args, **kwargs):
        events.append("enter")
        try:
            yield True
        finally:
            events.append("exit")

    async def stalled_asr(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append("asr-cleaned")

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", tracked_lease)
    voice_input.asr.side_effect = stalled_asr
    task = asyncio.create_task(process_voice(voice_input))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert events == ["enter", "asr-cleaned", "exit"]

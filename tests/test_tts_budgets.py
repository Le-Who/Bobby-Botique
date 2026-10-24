"""Speech request budgets and lossless, sequential complete-message assembly."""

import asyncio
from contextlib import asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import voice_engine
from app.providers.tts_budget import bisect_speech_text, split_speech_text
from app.runtime_settings.lifecycle import runtime_settings_scope
from app.runtime_settings.store import SettingsSnapshot


def test_speech_split_preserves_words_unicode_and_every_separator():
    text = "Начало.  Ещё слово!\n\nCafé — 👨‍👩‍👧‍👦 рядом.   Last words."
    pieces = split_speech_text(text, 50)
    assert "".join(pieces) == text
    assert all(len(piece.encode("utf-8")) <= 50 for piece in pieces)
    assert all(not piece or piece[-1].isspace() for piece in pieces[:-1])
    assert any("👨‍👩‍👧‍👦" in piece for piece in pieces)


def test_cjk_sentences_without_spaces_split_at_sentence_boundaries():
    text = "你好世界。再见朋友。"
    assert split_speech_text(text, 15) == ["你好世界。", "再见朋友。"]


@pytest.mark.parametrize("text", ["word" * 100, "я" * 200, "Café" * 50])
def test_unbreakable_word_never_becomes_separate_spoken_fragments(text):
    from app.providers.tts_audio import TTSRequestError

    with pytest.raises(TTSRequestError, match="unsplittable_text"):
        split_speech_text(text, 80)


@pytest.mark.parametrize("text", ["слово " * 1000, "界 " * 1000, "🐱 " * 1000])
def test_long_unicode_text_stays_under_each_piece_bound_without_losing_whitespace(text):
    pieces = split_speech_text(text, 160)
    assert all(0 < len(piece.encode("utf-8")) <= 160 for piece in pieces)
    assert "".join(pieces) == text


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_local_chunk_bound_is_rejected(limit):
    with pytest.raises(ValueError):
        split_speech_text("Hello.", max_bytes=limit)


def test_adaptive_bisection_keeps_full_source_and_never_retries_an_atomic_word():
    assert bisect_speech_text("One two.  Three four.") == ["One two.  ", "Three four."]
    assert bisect_speech_text("unbreakable") is None


def install_keys(monkeypatch):
    suspended = []

    async def key(*args, excluded_hashes, **kwargs):
        return {"key_hash": "healthy", "api_key": "offline"} if "healthy" not in excluded_hashes else None

    async def suspend(*args):
        suspended.append(args)

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    return suspended


@pytest.mark.asyncio
async def test_incomplete_piece_is_bisected_sequentially_without_key_penalty(monkeypatch):
    from app.providers.tts_audio import TTSOutputError

    suspended = install_keys(monkeypatch)
    text = "First sentence.  Second sentence."
    seen, progress = [], []
    inflight = 0

    async def synthesize(transcript, *args, **kwargs):
        nonlocal inflight
        assert inflight == 0
        inflight += 1
        try:
            seen.append(transcript)
            await asyncio.sleep(0)
            if transcript == text:
                raise TTSOutputError("incomplete_audio")
            return b"\x01\x00" if transcript.startswith("First") else b"\x02\x00"
        finally:
            inflight -= 1

    async def complete(done, total):
        progress.append((done, total))

    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    result = await voice_engine._run_gemini_pipeline([text], "Aoede", 1, on_chunk_complete=complete)
    assert result == [b"\x01\x00", b"\x02\x00"]
    assert seen == [text, "First sentence.  ", "Second sentence."]
    assert progress[-1] == (2, 2)
    assert suspended == []


@pytest.mark.asyncio
async def test_repeated_truncation_is_bounded_and_does_not_return_partial_pcm(monkeypatch):
    from app.providers.tts_audio import TTSOutputError

    install_keys(monkeypatch)
    seen = []

    async def synthesize(text, *args, **kwargs):
        seen.append(text)
        if text == "Successful first.":
            return b"\x01\x00"
        raise TTSOutputError("incomplete_audio")

    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    assert await voice_engine._run_gemini_pipeline(["Successful first.", "Cannot finish."], "Aoede", 1) is None
    assert seen[0] == "Successful first."
    assert seen.count("Cannot finish.") == 1
    assert len(seen) <= 5


@pytest.mark.asyncio
async def test_low_output_setting_shrinks_requests_and_pcm_join_keeps_every_sample(monkeypatch):
    from app.process_policies import ResolvedPolicy
    from app.runtime_settings.audio import baseline_config

    install_keys(monkeypatch)
    text = "First sentence with several words.  Ещё предложение для проверки. " * 20
    seen, encoded, snapshots = [], [], []

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("gemini",))
            if process_id == "tts.delivery"
            else ResolvedPolicy((baseline[0],), "sequential", True, 1)
        )

    async def synthesize(transcript, *args, **kwargs):
        seen.append(transcript)
        snapshots.append(kwargs["speech_settings"])
        return (len(seen)).to_bytes(2, "little") * 1000

    async def encode(pcm):
        encoded.append(pcm)
        return b"one-ogg"

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", encode)
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    config = baseline_config("gemini")
    config["max_output_tokens"] = 1024
    job = SimpleNamespace(response_text=text.strip(), voice="Aoede", tts_temperature=None)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({"audio:gemini": config}))):
        assert await manager._pregenerate_audio(job) == b"one-ogg"
    assert len(seen) > 2
    assert all(len(piece.encode("utf-8")) <= 163 for piece in seen)
    assert "".join(seen) == text.strip()
    assert len({id(snapshot) for snapshot in snapshots}) == 1
    assert snapshots[0].voice == "Aoede" and snapshots[0].max_output_tokens == 1024
    assert encoded == [b"".join(index.to_bytes(2, "little") * 1000 for index in range(1, len(seen) + 1))]


@pytest.mark.asyncio
async def test_failed_later_piece_delivers_no_partial_voice(monkeypatch):
    from app.process_policies import ResolvedPolicy

    install_keys(monkeypatch)
    text = "This sentence is repeated to require several bounded requests. " * 100
    requests = []

    async def resolve(process_id, baseline):
        return (
            ResolvedPolicy(("gemini",))
            if process_id == "tts.delivery"
            else ResolvedPolicy((baseline[0],), "sequential", True, 1)
        )

    async def synthesize(transcript, *args, **kwargs):
        requests.append(transcript)
        return b"\x01\x00" * 100 if len(requests) == 1 else None

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    encode = AsyncMock(return_value=b"partial-ogg")
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", encode)
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    bot = SimpleNamespace(send_voice=AsyncMock(), send_chat_action=AsyncMock())
    job = voice_engine.VoiceJob("job", 1, 1, 1, text, "Aoede", None, "source", bot, 1)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        await manager._process_job_leased(job)
    assert len(requests) == 2
    encode.assert_not_awaited()
    bot.send_voice.assert_not_awaited()
    assert manager._set_status.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("already_clean,expected", [(False, "keep"), (True, "https://example.test keep")])
async def test_precleaned_job_preserves_cleanup_output_without_a_second_pass(monkeypatch, already_clean, expected):
    from app.process_policies import ResolvedPolicy
    from app.providers.tts import _clean_text_for_speech

    install_keys(monkeypatch)
    cleaned_once = _clean_text_for_speech("https**://example.test keep")
    assert cleaned_once == "https://example.test keep"
    seen = []

    async def resolve(process_id, baseline):
        return ResolvedPolicy(("gemini",)) if process_id == "tts.delivery" else ResolvedPolicy(baseline)

    async def synthesize(transcript, *args, **kwargs):
        seen.append(transcript)
        return b"\x01\x00"

    monkeypatch.setattr(voice_engine, "resolve_process", resolve)
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    manager._set_status = AsyncMock()
    job = SimpleNamespace(response_text=cleaned_once, voice="Aoede", tts_temperature=None, text_is_clean=already_clean)
    async with runtime_settings_scope(SettingsSnapshot(1, MappingProxyType({}))):
        assert await manager._pregenerate_audio(job) == b"ogg"
    assert seen == [expected]


@pytest.mark.asyncio
async def test_public_queue_entry_preserves_precleaned_provenance_flag(monkeypatch):
    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    manager = voice_engine.VoiceReplyManager()
    monkeypatch.setattr(voice_engine, "_voice_reply_manager", manager)
    manager._set_status = AsyncMock()
    captured = []

    async def process(job):
        captured.append((job.text_is_clean, job.response_text, job.reply_to_message_id))

    manager._process_job = process
    try:
        result = await voice_engine.fire_voice_reply(
            bot=SimpleNamespace(),
            user_id=1,
            chat_id=1,
            reply_to_message_id=23,
            response_text="https://example.test keep",
            source_key="source",
            expected_epoch=1,
            text_is_clean=True,
        )
        assert result.queued
        await manager.wait_until_idle(1)
    finally:
        await manager.close()
    assert captured == [(True, "https://example.test keep", 23)]

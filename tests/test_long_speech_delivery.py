"""Physical source parts reach real FIFO jobs without needless speech pieces."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import voice_engine
from app.handlers.msg_speech import handle_speech_request, speech_request_for_message
from app.metrics import MetricsCollector
from app.middleware.debounce import DebounceResult
from app.process_policies import ResolvedPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("source_count", [3, 4, 8])
async def test_long_source_parts_keep_replies_content_and_fifo_without_unneeded_tts_parts(monkeypatch, source_count):
    """A slow first part gates identical later sources, with no chat generation."""
    from app.runtime_settings.store import SettingsSnapshot
    from tests.factories import make_chat_state
    from tests.test_voice_engine import FakeBot

    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield True

    manager = voice_engine.VoiceReplyManager()
    bot = FakeBot()
    collector = MetricsCollector()
    release = asyncio.Event()
    started = asyncio.Event()
    seen = []
    encoded = []
    inflight = 0

    async def synthesize(text, *_args, **_kwargs):
        nonlocal inflight
        assert inflight == 0
        inflight += 1
        try:
            seen.append(text)
            if len(seen) == 1:
                started.set()
                await release.wait()
            await asyncio.sleep(0)
            return len(seen).to_bytes(2, "little") * 1000
        finally:
            inflight -= 1

    async def encode(pcm):
        encoded.append(pcm)
        return b"synthetic-ogg-" + len(encoded).to_bytes(1, "little")

    async def policy(process_id, baseline):
        return ResolvedPolicy(("gemini",)) if process_id == "tts.delivery" else ResolvedPolicy((baseline[0],))

    async def available_key(*_args, **_kwargs):
        return {"key_hash": "synthetic", "api_key": "offline"}

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr("app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(1, {})))
    chat_state = make_chat_state()
    chat_state.voice_id = "Aoede"
    chat_state.tts_temperature = 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat_state))
    monkeypatch.setattr(voice_engine, "fire_voice_reply", manager.enqueue)
    monkeypatch.setattr(voice_engine, "resolve_process", policy)
    monkeypatch.setattr("app.providers.tts.generate_speech", synthesize)
    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", available_key)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=AsyncMock()))
    monkeypatch.setattr("app.utils.audio.pcm_to_ogg_opus", encode)
    monkeypatch.setattr("app.handlers.msg_speech.metrics_collector", collector)
    generation = AsyncMock(side_effect=AssertionError("direct speech called chat generation"))
    monkeypatch.setattr("app.handlers.agent.process_long_request", generation)

    first = "Озвучь: " + "Начало. " * 510 + " электро"
    assert len(first) == 4096
    repeated = "станция. " + "Одинаковый фрагмент. " * 120
    source_ids = list(range(101, 101 + source_count))
    source_texts = [first, *([repeated] * (source_count - 1))]
    messages = [
        SimpleNamespace(text=text, message_id=number, reply_text=AsyncMock(), reply_to_message=None)
        for number, text in zip(source_ids, source_texts, strict=True)
    ]
    entries = [SimpleNamespace(text=item.text, message=item) for item in messages]
    batch = DebounceResult(entries=entries, speech_collection=True, speech_epoch=0)
    request = speech_request_for_message(messages[0], batch)
    original = first.removeprefix("Озвучь: ") + repeated * (source_count - 1)
    assert request.action == "speak"
    assert "".join(part.text for part in request.parts) == original
    assert [part.reply_to_message_id for part in request.parts] == source_ids
    assert request.parts[1].text.startswith("электростанция.")

    update = SimpleNamespace(
        effective_message=messages[0],
        effective_user=SimpleNamespace(id=11),
        effective_chat=SimpleNamespace(id=11),
    )
    try:
        await handle_speech_request(update, SimpleNamespace(bot=bot), request)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert len(seen) == 1
        bot.send_voice.assert_not_awaited()
        release.set()
        await manager.wait_until_idle(11, timeout=5)
        sent = bot.send_voice.await_args_list
        assert [call.kwargs["reply_to_message_id"] for call in sent] == source_ids
        assert len(encoded) == source_count
        cursor = 0
        for part, pcm in zip(request.parts, encoded, strict=True):
            count = len(pcm) // 2000
            assert count == 1
            assert "".join(seen[cursor : cursor + count]).strip() == part.text.strip()
            assert pcm == b"".join(
                index.to_bytes(2, "little") * 1000 for index in range(cursor + 1, cursor + count + 1)
            )
            cursor += count
        assert cursor == len(seen)
        generation.assert_not_awaited()
        collector._drain_events()
        assert collector.metrics.request_count == 1
        assert collector.metrics.api_calls == {}
        assert manager._retained_jobs == manager._retained_bytes == 0
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("middle_code", [False, True])
async def test_formatting_spanning_source_cut_is_cleaned_once_with_original_reply_ids(monkeypatch, middle_code):
    from tests.factories import make_chat_state

    chat_state = make_chat_state()
    chat_state.voice_id, chat_state.tts_temperature = "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat_state))
    enqueue = AsyncMock(return_value=SimpleNamespace(queued=True, deduped=False))
    monkeypatch.setattr(voice_engine, "fire_voice_reply", enqueue)
    monkeypatch.setattr("app.handlers.msg_speech.metrics_collector", MetricsCollector())
    prefix = "Озвучь: Вступление. ```python\n"
    first = prefix + ("Заключение. value = 123; " * 300)[: 4096 - len(prefix)]
    texts = [first] + (["value = 123; " * 300] if middle_code else []) + [" tail_of_code\n```\nЗаключение."]
    messages = [
        SimpleNamespace(text=text, message_id=number, reply_text=AsyncMock())
        for number, text in zip(range(101, 101 + len(texts)), texts, strict=True)
    ]
    batch = DebounceResult(
        [SimpleNamespace(text=item.text, message=item) for item in messages], speech_collection=True, speech_epoch=0
    )
    request = speech_request_for_message(messages[0], batch)
    update = SimpleNamespace(
        effective_message=messages[0], effective_user=SimpleNamespace(id=11), effective_chat=SimpleNamespace(id=11)
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await handle_speech_request(update, SimpleNamespace(bot=bot), request)
    calls = enqueue.await_args_list
    assert [call.kwargs["reply_to_message_id"] for call in calls] == [101, 100 + len(texts)]
    assert [call.kwargs["response_text"].strip() for call in calls] == ["Вступление.", "Заключение."]
    assert all(call.kwargs["text_is_clean"] for call in calls)
    if middle_code:
        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["reply_to_message_id"] == 102
        assert "нет текста для озвучки" in bot.send_message.await_args.kwargs["text"]
    else:
        bot.send_message.assert_not_awaited()

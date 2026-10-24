"""A short final continuation can share its previous source's one speech job."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.msg_speech import handle_speech_request
from app.metrics import MetricsCollector
from app.voice_intent import SpeechRequestIntent, SpeechSourcePart


@pytest.fixture
def speech_tail(monkeypatch):
    active = []
    lease_calls = []

    @asynccontextmanager
    async def lease(user_id, epoch, **kwargs):
        lease_calls.append((user_id, epoch, kwargs))
        active.append(True)
        try:
            yield True
        finally:
            active.pop()

    receipt = object()

    async def plan(**kwargs):
        assert active, "private source must not reach a remote counter outside its epoch lease"
        return SimpleNamespace(allowed=True, token_receipts=(receipt,))

    planner = AsyncMock(side_effect=plan)
    enqueue = AsyncMock(return_value=SimpleNamespace(queued=True, deduped=False, reason_code=None))
    chat = SimpleNamespace(voice_id="Aoede", tts_temperature=0.3, memory_epoch=7)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.voice_engine.plan_voice_source_merge", planner, raising=False)
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    collector = MetricsCollector()
    monkeypatch.setattr("app.handlers.msg_speech.metrics_collector", collector)
    bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(message_id=101, text="Озвучь: длинный текст", reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message, effective_user=SimpleNamespace(id=11), effective_chat=SimpleNamespace(id=11)
    )
    return SimpleNamespace(
        planner=planner,
        enqueue=enqueue,
        bot=bot,
        update=update,
        receipt=receipt,
        collector=collector,
        lease_calls=lease_calls,
    )


def request(*texts, contiguous=True):
    return SpeechRequestIntent(
        "speak",
        text="".join(texts),
        source="inline",
        expected_epoch=7,
        contiguous_parts=contiguous,
        parts=tuple(SpeechSourcePart(text, 101 + index) for index, text in enumerate(texts)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("full_count", [1, 2, 3, 4, 7])
async def test_short_final_tail_joins_only_previous_full_source_and_reuses_token_receipt(speech_tail, full_count):
    full = "Полное предложение с точным текстом. " * 100
    tail = "Короткий конец. Ещё одна фраза."
    item = request(*([full] * full_count), tail)
    await handle_speech_request(speech_tail.update, SimpleNamespace(bot=speech_tail.bot), item)
    calls = speech_tail.enqueue.await_args_list
    assert [call.kwargs["reply_to_message_id"] for call in calls] == list(range(101, 101 + full_count))
    assert calls[-1].kwargs["response_text"] == full + tail
    assert calls[-1].kwargs["token_receipts"] == (speech_tail.receipt,)
    assert calls[-1].kwargs["expected_epoch"] == 7
    speech_tail.planner.assert_awaited_once()
    assert speech_tail.planner.await_args.kwargs["response_text"] == full + tail
    assert speech_tail.lease_calls[0][:2] == (11, 7)
    speech_tail.bot.send_message.assert_awaited_once()
    assert speech_tail.bot.send_message.await_args.kwargs["reply_to_message_id"] == 101 + full_count
    speech_tail.collector._drain_events()
    assert speech_tail.collector.metrics.request_count == 1
    assert speech_tail.collector.metrics.api_calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["not_fit", "counter_unavailable"])
async def test_tail_keeps_own_reply_when_combined_request_cannot_be_admitted(speech_tail, reason):
    speech_tail.planner.side_effect = None if reason == "not_fit" else OSError("counter unavailable")
    speech_tail.planner.return_value = SimpleNamespace(allowed=False, token_receipts=())
    full, tail = "Основной текст. " * 250, "Последняя фраза."
    await handle_speech_request(speech_tail.update, SimpleNamespace(bot=speech_tail.bot), request(full, tail))
    assert [call.kwargs["reply_to_message_id"] for call in speech_tail.enqueue.await_args_list] == [101, 102]
    speech_tail.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["both_full", "independent", "many_words", "large_ratio"])
async def test_independent_or_substantial_sources_do_not_merge(speech_tail, kind):
    full = "Основной текст. " * 250
    tail = full if kind == "both_full" else "Короткий конец."
    if kind == "many_words":
        tail = "я " * 81
    if kind == "large_ratio":
        tail = "длинноеслово " * 65
        assert len(tail.strip()) * 5 > len(full.strip())
    await handle_speech_request(
        speech_tail.update,
        SimpleNamespace(bot=speech_tail.bot),
        request(full, tail, contiguous=kind != "independent"),
    )
    speech_tail.planner.assert_not_awaited()
    assert [call.kwargs["reply_to_message_id"] for call in speech_tail.enqueue.await_args_list] == [101, 102]


@pytest.mark.asyncio
async def test_rejected_merged_job_reports_both_original_sources_without_false_merge_notice(speech_tail):
    speech_tail.enqueue.return_value = SimpleNamespace(queued=False, deduped=False, reason_code="capacity")
    await handle_speech_request(
        speech_tail.update, SimpleNamespace(bot=speech_tail.bot), request("Основной текст. " * 250, "Короткий конец.")
    )
    speech_tail.enqueue.assert_awaited_once()
    speech_tail.update.effective_message.reply_text.assert_awaited_once()
    speech_tail.bot.send_message.assert_awaited_once()
    assert speech_tail.bot.send_message.await_args.kwargs["reply_to_message_id"] == 102
    assert "заполнена" in speech_tail.bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_cancelled_private_merge_planning_never_enqueues_or_claims_completion(speech_tail):
    speech_tail.planner.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await handle_speech_request(
            speech_tail.update,
            SimpleNamespace(bot=speech_tail.bot),
            request("Основной текст. " * 250, "Короткий конец."),
        )
    speech_tail.enqueue.assert_not_awaited()
    speech_tail.bot.send_message.assert_not_awaited()
    speech_tail.collector._drain_events()
    assert speech_tail.collector.metrics.request_count == 0

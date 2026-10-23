"""Direct speech bypasses chat, classifiers and draw routing at ingress."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import agent
from app.middleware.debounce import DebounceResult
from tests import test_edited_request_ownership as support

pytestmark = pytest.mark.asyncio
ownership = support.ownership


@pytest.fixture
def speech(monkeypatch):
    from tests.factories import make_chat_state

    chat = make_chat_state()
    chat.voice_id = "Aoede"
    chat.tts_temperature = 0.3
    get_chat = AsyncMock(return_value=chat)
    enqueue = AsyncMock(return_value=SimpleNamespace(queued=True, deduped=False, reason_code=None))
    generate = AsyncMock()
    monkeypatch.setattr("app.repos.chats.get_user_chat", get_chat)
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    monkeypatch.setattr(agent, "process_long_request", generate)
    return SimpleNamespace(enqueue=enqueue, generate=generate, get_chat=get_chat)


@pytest.mark.parametrize("edited", [False, True])
async def test_inline_speech_does_not_generate_or_echo_text(monkeypatch, ownership, speech, edited):
    classifier = AsyncMock()
    draw = AsyncMock()
    monkeypatch.setattr("app.voice_intent.detect_tts_intent", classifier)
    monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", draw)
    update = ownership.update("Озвучь: АААААААА", edited=edited)
    update.effective_message.message_id = 9
    await ownership.launch(update)
    await support.settle()
    speech.enqueue.assert_awaited_once()
    assert speech.enqueue.await_args.kwargs["response_text"] == "АААААААА"
    assert speech.enqueue.await_args.kwargs["reply_to_message_id"] == 9
    assert speech.enqueue.await_args.kwargs["voice"] == "Aoede"
    speech.generate.assert_not_awaited()
    classifier.assert_not_awaited()
    draw.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS


async def test_reply_speech_uses_exact_source(ownership, speech):
    update = ownership.update("Озвучь это сообщение")
    update.effective_message.message_id = 9
    update.effective_message.reply_to_message = SimpleNamespace(text="Текст с исходным регистром!", caption=None)
    await ownership.launch(update)
    speech.enqueue.assert_awaited_once()
    assert speech.enqueue.await_args.kwargs["response_text"] == "Текст с исходным регистром!"
    speech.generate.assert_not_awaited()


async def test_reply_to_caption_is_a_source(ownership, speech):
    update = ownership.update("Прочитай вслух")
    update.effective_message.message_id = 9
    update.effective_message.reply_to_message = SimpleNamespace(text=None, caption="Подпись под фото")
    await ownership.launch(update)
    assert speech.enqueue.await_args.kwargs["response_text"] == "Подпись под фото"


async def test_transcript_topics_do_not_trigger_natal_routing(monkeypatch, ownership, speech):
    natal = AsyncMock()
    monkeypatch.setattr("app.handlers.natal_chart.natal_command", natal)
    update = ownership.update("Озвучь: Натальная карта — это рисунок неба.")
    update.effective_message.message_id = 9
    await ownership.launch(update)
    assert speech.enqueue.await_args.kwargs["response_text"] == "Натальная карта — это рисунок неба."
    natal.assert_not_awaited()


async def test_forwarded_command_alone_is_not_user_intent(monkeypatch, ownership, speech):
    update = ownership.update("Озвучь: чужая команда")
    update.effective_message.forward_origin = object()
    await ownership.launch(update)
    await support.settle()
    speech.enqueue.assert_not_awaited()
    speech.generate.assert_awaited_once()


async def test_cancelled_enqueue_releases_admission(ownership, speech):
    import asyncio

    started = asyncio.Event()

    async def enqueue(**kwargs):
        started.set()
        await asyncio.Event().wait()

    speech.enqueue.side_effect = enqueue
    update = ownership.update("Озвучь: Текст")
    update.effective_message.message_id = 9
    owner = ownership.launch(update)
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert not state.get_user_state(11).is_processing
    assert 11 not in state._ACTIVE_TASKS
    speech.generate.assert_not_awaited()


async def test_forward_batch_reads_source_without_attribution(monkeypatch, ownership, speech):
    update = ownership.update("Озвучь пересланное сообщение")
    update.effective_message.message_id = 9
    forwarded = SimpleNamespace(
        text="Пересланный Текст",
        is_forwarded=True,
        is_user_authored=False,
        forwarded_date=None,
        message=SimpleNamespace(photo=(), reply_to_message=None),
        author_label="Synthetic participant",
    )
    command = SimpleNamespace(
        text=update.effective_message.text,
        is_forwarded=False,
        is_user_authored=True,
        message=update.effective_message,
    )
    monkeypatch.setattr(
        "app.middleware.debounce.debounce_message", AsyncMock(return_value=DebounceResult([forwarded, command]))
    )
    await ownership.launch(update)
    assert speech.enqueue.await_args.kwargs["response_text"] == "Пересланный Текст"
    speech.generate.assert_not_awaited()


async def test_missing_source_asks_without_model_call(ownership, speech):
    update = ownership.update("Озвучь этот текст")
    await ownership.launch(update)
    update.effective_message.reply_text.assert_awaited_once()
    speech.enqueue.assert_not_awaited()
    speech.generate.assert_not_awaited()


@pytest.mark.parametrize("reason", ["capacity", "stale_epoch", "text_too_large"])
async def test_enqueue_rejection_is_visible_without_chat_fallback(ownership, speech, reason):
    speech.enqueue.return_value = SimpleNamespace(queued=False, deduped=False, reason_code=reason)
    update = ownership.update("Озвучь: Точный текст")
    update.effective_message.message_id = 9
    await ownership.launch(update)
    update.effective_message.reply_text.assert_awaited_once()
    speech.generate.assert_not_awaited()
    assert not state.get_user_state(11).is_processing

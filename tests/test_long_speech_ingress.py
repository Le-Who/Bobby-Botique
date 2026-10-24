"""Physical Telegram speech parts keep their identity, seams and admission outcome."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.msg_speech import handle_speech_request, speech_request_for_message
from app.middleware import debounce, dedup
from app.middleware.debounce import DebounceResult
from tests import test_edited_request_ownership as support

real_debounce_message = debounce.debounce_message
ownership = support.ownership


@pytest.fixture(autouse=True)
def _individual_source_admission(monkeypatch):
    """Ownership regressions exercise the valid, unmerged admission outcome."""

    @asynccontextmanager
    async def lease(*_args, **_kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr(
        "app.voice_engine.plan_voice_source_merge",
        AsyncMock(return_value=SimpleNamespace(allowed=False, token_receipts=())),
        raising=False,
    )


def message(text, message_id, *, chat_id=11, forwarded=False):
    return SimpleNamespace(
        text=text,
        caption=None,
        message_id=message_id,
        chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id, type="private"),
        photo=(),
        voice=None,
        forward_origin=SimpleNamespace(type="hidden_user", sender_user_name="Source", date=None) if forwarded else None,
        reply_to_message=None,
        reply_text=AsyncMock(),
    )


def entry(item):
    return SimpleNamespace(
        message=item,
        text=item.text,
        is_user_authored=not item.forward_origin,
        is_forwarded=bool(item.forward_origin),
    )


@pytest.mark.asyncio
async def test_distinct_identical_physical_messages_are_not_redelivery():
    dedup.clear_user_dedup(11)
    assert not await dedup.is_duplicate_request(11, "одинаковый фрагмент", chat_id=11, message_id=101)
    assert not await dedup.is_duplicate_request(11, "одинаковый фрагмент", chat_id=11, message_id=102)
    assert await dedup.is_duplicate_request(11, "одинаковый фрагмент", chat_id=11, message_id=101)
    dedup.clear_user_dedup(11)


@pytest.mark.asyncio
async def test_delayed_long_speech_collects_raw_commandlike_continuation(monkeypatch):
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.2, raising=False)
    first = message("Озвучь: " + "а " * 2044, 101)
    second = message("Озвучь ответ про погоду — это продолжение текста.", 102)
    owner = asyncio.create_task(debounce.debounce_message(11, first, speech_epoch=7))
    try:
        await asyncio.sleep(0.02)
        assert not owner.done(), "the old ordinary debounce flushed before the delayed source arrived"
        assert await debounce.debounce_message(11, second, speech_epoch=7) is None
        result = await owner
        request = speech_request_for_message(first, result)
        assert request.action == "speak"
        assert request.expected_epoch == 7
        assert [part.reply_to_message_id for part in request.parts] == [101, 102]
        assert "Озвучь ответ про погоду" in "".join(part.text for part in request.parts)
        first.reply_text.assert_awaited()
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


def test_physical_seam_moves_whole_word_without_inserting_or_losing_text():
    prefix = "Начало. " + "а" * 4075 + " "
    first = message("Озвучь: " + prefix + "пере", 101)
    second = message("несённое слово. ", 102)
    third = message("Точный конец.", 103)
    batch = DebounceResult([entry(first), entry(second), entry(third)], speech_collection=True)
    request = speech_request_for_message(first, batch)
    assert [part.text for part in request.parts] == [prefix, "перенесённое слово. ", "Точный конец."]
    assert "".join(part.text for part in request.parts) == prefix + "перенесённое слово. Точный конец."
    assert [part.reply_to_message_id for part in request.parts] == [101, 102, 103]


@pytest.mark.parametrize(
    ("left", "right", "word"),
    [
        ("Cafe", "\u0301 encore.", "Cafe\u0301 encore."),
        ("пере-", "нос дальше.", "пере-нос дальше."),
        ("l'", "esprit continue.", "l'esprit continue."),
    ],
)
def test_unicode_lexical_seams_are_not_left_in_separate_audio(left, right, word):
    prefix = "Начало. " + "а" * (4079 - len(left)) + " "
    first, second = message("Озвучь: " + prefix + left, 101), message(right, 102)
    request = speech_request_for_message(first, DebounceResult([entry(first), entry(second)], speech_collection=True))
    assert [part.text for part in request.parts] == [prefix, word]


def test_atomic_token_spanning_complete_source_rejects_before_partial_audio():
    first, second = message("Озвучь: " + "А" * 4088, 101), message("ААА конец.", 102)
    request = speech_request_for_message(first, DebounceResult([entry(first), entry(second)], speech_collection=True))
    assert request.action == "clarify"
    assert request.error == "unbreakable_seam"
    assert not request.parts


@pytest.mark.asyncio
async def test_astral_scalar_limit_starts_collection_and_shorter_text_does_not(monkeypatch):
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60, raising=False)
    assert not debounce.starts_long_speech(message("Озвучь: " + "а" * 2000, 100))
    first = message("Озвучь: " + "🙂 " * 1362 + "а ", 101)
    assert len(first.text.encode("utf-16-le")) // 2 == 4096
    owner = asyncio.create_task(debounce.debounce_message(11, first, speech_epoch=7))
    try:
        await asyncio.sleep(0.02)
        assert not owner.done()
        await debounce.debounce_message(11, message("Готово", 102), speech_epoch=7)
        result = await owner
        assert speech_request_for_message(first, result).action == "speak"
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_source_collection_is_owned_before_state_interceptors(monkeypatch, ownership):
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.2, raising=False)
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    observed = []

    async def enqueue(**kwargs):
        observed.append((kwargs["reply_to_message_id"], kwargs["expected_epoch"], kwargs["response_text"]))
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    from app import state
    from app.handlers import messages

    interceptor = AsyncMock(return_value=True)
    monkeypatch.setattr(messages, "handle_role_rename", interceptor)
    first = ownership.update("Озвучь: " + "а " * 2044)
    first.effective_message.chat_id, first.effective_message.message_id = 11, 101
    owner = ownership.launch(first)
    await asyncio.sleep(0.02)
    assert state.get_active_task(11) is owner
    second = ownership.update("/newchat — это текст, а не команда.")
    second.effective_message.chat_id, second.effective_message.message_id = 11, 102
    await ownership.launch(second)
    await owner
    assert observed == [(101, 7, "а " * 2044), (102, 7, "/newchat — это текст, а не команда.")]
    interceptor.assert_not_awaited()
    assert not state.get_user_state(11).is_processing


@pytest.mark.asyncio
async def test_overlapping_source_is_captured_while_initial_chat_snapshot_loads(monkeypatch, ownership):
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    snapshot_started, release_snapshot = asyncio.Event(), asyncio.Event()
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3

    async def load_chat(_uid):
        snapshot_started.set()
        await release_snapshot.wait()
        return chat

    monkeypatch.setattr("app.repos.chats.get_user_chat", load_chat)
    observed = []

    async def enqueue(**kwargs):
        observed.append((kwargs["reply_to_message_id"], kwargs["expected_epoch"], kwargs["response_text"]))
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    first, second = ownership.update("Озвучь: " + "а " * 2044), ownership.update("Сохранённый конец.")
    for item, source_id in ((first, 101), (second, 102)):
        item.effective_message.chat_id, item.effective_message.message_id = 11, source_id
    owner = ownership.launch(first)
    try:
        await snapshot_started.wait()
        assert debounce.has_open_speech_slot(11, 11)
        await ownership.launch(second)
        release_snapshot.set()
        await owner
        assert observed == [(101, 7, "а " * 2044), (102, 7, "Сохранённый конец.")]
    finally:
        release_snapshot.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["hydration", "authorization"])
async def test_source_hint_survives_continuation_authorized_before_first_source(monkeypatch, ownership, gate):
    from app import state
    from app.handlers import messages
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.03)
    first_gate, release_first = asyncio.Event(), asyncio.Event()
    continuation_admitted = asyncio.Event()
    capture_reserved = debounce.capture_reserved_speech_message

    async def capture(*args, **kwargs):
        continuation_admitted.set()
        return await capture_reserved(*args, **kwargs)

    monkeypatch.setattr(debounce, "capture_reserved_speech_message", capture)
    count = 0

    async def guarded(_uid):
        nonlocal count
        count += 1
        if count == 1:
            first_gate.set()
            await release_first.wait()
        return True

    monkeypatch.setattr(
        state if gate == "hydration" else messages,
        "ensure_state_loaded" if gate == "hydration" else "is_authorized",
        guarded,
    )
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    observed = []

    async def enqueue(**kwargs):
        observed.append(kwargs["reply_to_message_id"])
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    first, second = ownership.update("Озвучь: " + "а " * 2044), ownership.update("Полный конец.")
    for item, source_id in ((first, 101), (second, 102)):
        item.effective_message.chat_id, item.effective_message.message_id = 11, source_id
    first_owner = ownership.launch(first)
    try:
        await first_gate.wait()
        second_owner = ownership.launch(second)
        # Exercise the intended admission order rather than racing a 100 ms
        # collection deadline against logging/scheduler overhead in the suite.
        await asyncio.wait_for(continuation_admitted.wait(), timeout=5)
        assert not first_owner.done()
        release_first.set()
        await asyncio.gather(first_owner, second_owner)
        assert observed == [101, 102]
    finally:
        release_first.set()


@pytest.mark.asyncio
async def test_three_source_parts_spend_one_real_rate_operation_near_limit(monkeypatch, ownership):
    import time

    from app.handlers import messages
    from app.security import RateLimiter
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.1)
    limiter = RateLimiter(max_requests=30, window_seconds=60)
    limiter._user_requests[11].extend([time.time()] * 28)
    monkeypatch.setattr(messages, "check_user_rate_limit", limiter.check_rate_limit)
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    observed = []

    async def enqueue(**kwargs):
        observed.append(kwargs["reply_to_message_id"])
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    updates = [ownership.update("Озвучь: " + "а " * 2044), ownership.update("б " * 2048), ownership.update("Конец.")]
    for item, source_id in zip(updates, (101, 102, 103), strict=True):
        item.effective_message.chat_id, item.effective_message.message_id = 11, source_id
    owner = ownership.launch(updates[0])
    await asyncio.sleep(0.01)
    await ownership.launch(updates[1])
    await ownership.launch(updates[2])
    await owner
    assert observed == [101, 102, 103]
    assert len(limiter._user_requests[11]) == 29


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal", "admission"), [("Конец.", "allow"), ("Готово", "allow"), ("Конец.", "error")])
async def test_pending_middle_authorization_keeps_short_terminal_source_open(
    monkeypatch, ownership, terminal, admission
):
    from app.handlers import messages
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    middle_auth_started, release_middle = asyncio.Event(), asyncio.Event()
    calls = 0

    async def authorize(_uid):
        nonlocal calls
        calls += 1
        if calls == 2:
            middle_auth_started.set()
            await release_middle.wait()
            if admission == "error":
                raise RuntimeError("synthetic authorization unavailable")
        return True

    monkeypatch.setattr(messages, "is_authorized", authorize)
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    observed = []

    async def enqueue(**kwargs):
        observed.append(kwargs["reply_to_message_id"])
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    updates = [ownership.update("Озвучь: " + "а " * 2044), ownership.update("б " * 2048), ownership.update(terminal)]
    for item, source_id in zip(updates, (101, 102, 103), strict=True):
        item.effective_message.chat_id, item.effective_message.message_id = 11, source_id
    owner = ownership.launch(updates[0])
    try:
        await asyncio.sleep(0.01)
        middle = ownership.launch(updates[1])
        await middle_auth_started.wait()
        await ownership.launch(updates[2])
        await asyncio.sleep(0.02)
        assert not owner.done(), "a known pending physical source must block completion of its batch"
        release_middle.set()
        results = await asyncio.gather(owner, middle, return_exceptions=True)
        if admission == "error":
            assert isinstance(results[1], RuntimeError)
            assert observed == []
        else:
            assert observed == ([101, 102] if terminal == "Готово" else [101, 102, 103])
    finally:
        release_middle.set()


@pytest.mark.asyncio
async def test_short_tail_arriving_before_middle_does_not_exclude_earlier_source(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.02)
    first, middle, last = (
        message("Озвучь: " + "а " * 2044, 101),
        message("б " * 2048, 102),
        message("Конец текста.", 103),
    )
    owner = asyncio.create_task(debounce.debounce_message(11, first, speech_epoch=7))
    try:
        await asyncio.sleep(0)
        assert await debounce.debounce_message(11, last, speech_epoch=7) is None
        assert await debounce.debounce_message(11, middle, speech_epoch=7) is None
        request = speech_request_for_message(first, await owner)
        assert [part.reply_to_message_id for part in request.parts] == [101, 102, 103]
        assert request.text == "а " * 2044 + "б " * 2048 + "Конец текста."
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_ambiguity_retains_sources_until_optional_finish(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_TOTAL_S", 0.2)
    item = message("Озвучь: " + "а" * 4088, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    try:
        await asyncio.sleep(0.02)
        assert not owner.done()
        assert item.reply_text.await_count == 2
        await debounce.debounce_message(11, message("Готово", 102), speech_epoch=7)
        request = speech_request_for_message(item, await owner)
        assert request.action == "speak"
        assert request.parts[0].text == "а" * 4088
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_continuation_during_idle_notice_does_not_lose_wakeup(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_TOTAL_S", 1)
    idle_notice_started, release_notice = asyncio.Event(), asyncio.Event()
    first = message("Озвучь: " + "а " * 2043 + "аб", 101)
    notice = SimpleNamespace(delete=AsyncMock())
    calls = 0

    async def reply(_text):
        nonlocal calls
        calls += 1
        if calls == 2:
            idle_notice_started.set()
            await release_notice.wait()
        return notice

    first.reply_text.side_effect = reply
    owner = asyncio.create_task(debounce.debounce_message(11, first, speech_epoch=7))
    try:
        await idle_notice_started.wait()
        await debounce.debounce_message(11, message("в конец.", 102), speech_epoch=7)
        release_notice.set()
        batch = await asyncio.wait_for(asyncio.shield(owner), timeout=0.2)
        assert [part.reply_to_message_id for part in speech_request_for_message(first, batch).parts] == [101, 102]
    finally:
        release_notice.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_complete_near_limit_sentence_finalizes_automatically(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_TOTAL_S", 0.2)
    item = message("Озвучь: " + "а" * 4087 + ".", 101)
    request = speech_request_for_message(item, await debounce.debounce_message(11, item, speech_epoch=7))
    assert request.action == "speak"
    assert request.parts[0].text.endswith(".")


@pytest.mark.asyncio
async def test_absolute_deadline_accepts_a_complete_tail_already_collected(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_TOTAL_S", 0)
    item = message("Озвучь: " + "а" * 4087 + ".", 101)
    request = speech_request_for_message(item, await debounce.debounce_message(11, item, speech_epoch=7))
    assert request.action == "speak"


@pytest.mark.asyncio
async def test_collector_cancellation_clears_visible_status(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    item = message("Озвучь: " + "а " * 2044, 101)
    notice = SimpleNamespace(delete=AsyncMock())
    item.reply_text.return_value = notice
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    await asyncio.sleep(0)
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    notice.delete.assert_awaited_once()
    assert not debounce.has_open_speech_slot(11, 11)


@pytest.mark.asyncio
async def test_collection_capacity_reports_every_physical_source(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    monkeypatch.setattr(debounce, "_SPEECH_MAX_PARTS", 2)
    sources = [message("Озвучь: " + "а " * 2044, 101), message("б " * 2048, 102), message("Последняя часть.", 103)]
    owner = asyncio.create_task(debounce.debounce_message(11, sources[0], speech_epoch=7))
    try:
        await asyncio.sleep(0)
        await debounce.debounce_message(11, sources[1], speech_epoch=7)
        await debounce.debounce_message(11, sources[2], speech_epoch=7)
        request = speech_request_for_message(sources[0], await owner)
        assert request.error == "collection_capacity"
        bot = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(
            effective_message=sources[0], effective_user=SimpleNamespace(id=11), effective_chat=sources[0].chat
        )
        await handle_speech_request(update, SimpleNamespace(bot=bot), request)
        assert [call.kwargs["reply_to_message_id"] for call in bot.send_message.await_args_list] == [102, 103]
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_user_other_chat_or_reply_elsewhere_is_not_source_data(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    item = message("Озвучь: " + "а " * 2044, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    try:
        await asyncio.sleep(0)
        assert not debounce.accepts_speech_message(11, message("Обычный вопрос", 102, chat_id=22))
        unrelated = message("Новая тема", 103)
        unrelated.reply_to_message = SimpleNamespace(message_id=1)
        assert not debounce.accepts_speech_message(11, unrelated)
        await debounce.debounce_message(11, message("Короткий конец.", 104), speech_epoch=7)
        assert not debounce.accepts_speech_message(11, message("Обычный вопрос", 105))
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_unrelated_reply_passes_through_real_debounce_without_being_absorbed(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    item = message("Озвучь: " + "а " * 2044, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    try:
        await asyncio.sleep(0)
        unrelated = message("Новая тема", 103)
        unrelated.reply_to_message = SimpleNamespace(message_id=1)
        result = await debounce.debounce_message(11, unrelated)
        assert result is not None
        assert not result.speech_collection
        assert [item.message.message_id for item in result.entries] == [103]
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


def test_collection_source_order_uses_telegram_ids_after_concurrent_arrival():
    first, second, third = message("Озвучь: Начало. ", 101), message("Середина. ", 102), message("Конец.", 103)
    request = speech_request_for_message(
        first, DebounceResult([entry(first), entry(third), entry(second)], speech_collection=True)
    )
    assert [part.reply_to_message_id for part in request.parts] == [101, 102, 103]
    assert request.text == "Начало. Середина. Конец."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "/newchat",
        "/newchat@synthetic_bot",
        "/deleteme",
        "/deleteme CONFIRM",
        "/cancel",
        "/newchat — это текст продолжения.",
    ],
)
async def test_real_application_dispatch_preserves_control_escape_and_claims_commandlike_data(monkeypatch, text):
    from datetime import UTC, datetime

    from telegram import Update, User
    from telegram.ext import Application, CommandHandler

    from app.handlers import messages

    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    item = message("Озвучь: " + "а " * 2044, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    observed = []

    async def data(_update, _context):
        observed.append("data")

    async def command(_update, _context):
        observed.append("control")

    application = Application.builder().token("123456:synthetic-test-token").build()
    application.bot._bot_user = User(123456, "Synthetic", True, username="synthetic_bot")
    application._initialized = True
    application.add_handler(CommandHandler(["newchat", "deleteme", "cancel"], command))
    messages.register(application)
    monkeypatch.setattr(messages, "handle_request", data)
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 102,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": 11, "type": "private"},
                "from": {"id": 11, "first_name": "Synthetic", "is_bot": False},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
            },
        },
        application.bot,
    )
    try:
        await asyncio.sleep(0)
        await application.process_update(update)
        assert observed == (["data"] if " — " in text else ["control"])
        assert not debounce.accepts_speech_message(11, message("Другая пересылка", 103, forwarded=True))
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        await application.bot.shutdown()


@pytest.mark.asyncio
async def test_tarot_dispatch_keeps_same_user_lane_free_for_speech_continuation(monkeypatch, ownership):
    from datetime import UTC, datetime

    from telegram import Update, User
    from telegram.ext import Application, ExtBot

    from app import state
    from app.handlers import messages, tarot_chat
    from app.update_processor import UserScopedUpdateProcessor
    from tests.factories import make_chat_state

    monkeypatch.setattr(debounce, "debounce_message", real_debounce_message)
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    snapshot_started, release_snapshot = asyncio.Event(), asyncio.Event()
    chat = make_chat_state()
    chat.memory_epoch, chat.voice_id, chat.tts_temperature = 7, "Aoede", 0.3

    async def get_chat(_uid):
        snapshot_started.set()
        await release_snapshot.wait()
        return chat

    monkeypatch.setattr("app.repos.chats.get_user_chat", get_chat)
    monkeypatch.setattr(state, "is_in_tarot_mode", lambda _uid: True)
    monkeypatch.setattr(tarot_chat, "is_in_tarot_mode", lambda _uid: True)
    tarot = AsyncMock()
    monkeypatch.setattr(tarot_chat, "handle_tarot_message", tarot)
    monkeypatch.setattr(ExtBot, "send_message", AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())))
    monkeypatch.setattr(ExtBot, "send_chat_action", AsyncMock())
    monkeypatch.setattr(ExtBot, "set_message_reaction", AsyncMock())
    queued = []

    async def enqueue(**kwargs):
        queued.append(kwargs["reply_to_message_id"])
        return SimpleNamespace(queued=True, deduped=False, reason_code=None)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    application = Application.builder().token("123456:synthetic-test-token").build()
    application.bot._bot_user = User(123456, "Synthetic", True, username="synthetic_bot")
    application._initialized = application._running = True
    messages.register(application)
    processor = UserScopedUpdateProcessor(4)

    def update(text, source_id):
        return Update.de_json(
            {
                "update_id": source_id,
                "message": {
                    "message_id": source_id,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": 11, "type": "private"},
                    "from": {"id": 11, "first_name": "Synthetic", "is_bot": False},
                    "text": text,
                },
            },
            application.bot,
        )

    first, last = update("Озвучь: " + "а " * 2044, 101), update("Конец текста.", 102)
    try:
        await asyncio.wait_for(processor.process_update(first, application.process_update(first)), timeout=0.2)
        await support.settle()
        tarot.assert_not_awaited()
        await asyncio.wait_for(snapshot_started.wait(), timeout=0.2)
        await asyncio.wait_for(processor.process_update(last, application.process_update(last)), timeout=0.2)
        release_snapshot.set()
        await asyncio.gather(*tuple(application._Application__create_task_tasks))
        assert queued == [101, 102]
        tarot.assert_not_awaited()
    finally:
        release_snapshot.set()
        await asyncio.gather(*tuple(application._Application__create_task_tasks), return_exceptions=True)
        application._running = False
        await application.bot.shutdown()


@pytest.mark.asyncio
async def test_desktop_soft_cut_collects_delayed_whole_words_and_restores_one_separator(monkeypatch):
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 0.005)
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.2)
    source = "Начало. " + "слово " * 495 + "Конец"
    first, last = message("Озвучь: " + source, 101), message("Следующее слово.", 102)
    assert 2049 < len(first.text) < 4094
    owner = asyncio.create_task(debounce.debounce_message(11, first, speech_epoch=7))
    try:
        await asyncio.sleep(0.02)
        assert not owner.done()
        await debounce.debounce_message(11, last, speech_epoch=7)
        request = speech_request_for_message(first, await owner)
        assert [part.text for part in request.parts] == [source + " ", "Следующее слово."]
        assert request.text == source + " Следующее слово."
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


def test_hard_cut_moves_only_last_lexical_token_after_adjacent_punctuation():
    first, last = message("Озвучь: " + "Да." * 1361 + "перен", 101), message("ос.", 102)
    assert len(first.text) == 4096
    request = speech_request_for_message(first, DebounceResult([entry(first), entry(last)], speech_collection=True))
    assert request.action == "speak"
    assert [part.text for part in request.parts] == ["Да." * 1361, "перенос."]


@pytest.mark.asyncio
async def test_epoch_change_rejects_collected_sources_before_enqueue(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60)
    item = message("Озвучь: " + "а " * 2044, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    try:
        await asyncio.sleep(0)
        await debounce.debounce_message(11, message("Конец.", 102), speech_epoch=8)
        request = speech_request_for_message(item, await owner)
        assert request.action == "clarify"
        assert request.error == "stale_epoch"
        assert not request.text
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_full_sized_tail_timeout_is_visible_and_never_falls_back_to_chat(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 0.01, raising=False)
    monkeypatch.setattr(debounce, "_SPEECH_TOTAL_S", 0.03, raising=False)
    item = message("Озвучь: " + "а" * 4088, 101)
    batch = await debounce.debounce_message(11, item, speech_epoch=7)
    request = speech_request_for_message(item, batch)
    assert request.action == "clarify"
    assert request.error == "collection_incomplete"
    assert not request.text
    item.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_explicit_finish_keeps_all_full_sized_parts(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60, raising=False)
    item = message("Озвучь: " + "а" * 4088, 101)
    owner = asyncio.create_task(debounce.debounce_message(11, item, speech_epoch=7))
    try:
        await asyncio.sleep(0)
        assert await debounce.debounce_message(11, message("Готово", 102), speech_epoch=7) is None
        batch = await owner
        request = speech_request_for_message(item, batch)
        assert request.action == "speak"
        assert len(request.parts) == 1
        assert request.parts[0].text == "а" * 4088
        assert request.parts[0].reply_to_message_id == 101
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_reset_cancels_and_removes_private_collected_sources(monkeypatch):
    monkeypatch.setattr(debounce, "_SPEECH_WINDOW_S", 60, raising=False)
    owner = asyncio.create_task(debounce.debounce_message(11, message("Озвучь: " + "а" * 4088, 101), speech_epoch=7))
    try:
        await asyncio.sleep(0)
        dedup.clear_user_dedup(11)
        await asyncio.gather(owner, return_exceptions=True)
        assert owner.cancelled()
        assert not debounce.has_open_speech_slot(11, 11)
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_forwarded_parts_keep_each_forward_reply_and_report_partial_capacity(monkeypatch):
    from tests.factories import make_chat_state

    command = message("Озвучь эти сообщения", 110)
    sources = [
        message("Один", 101, forwarded=True),
        message("Один", 102, forwarded=True),
        message("Три", 103, forwarded=True),
    ]
    request = speech_request_for_message(command, DebounceResult([*(entry(item) for item in sources), entry(command)]))
    observed = []

    async def enqueue(**kwargs):
        observed.append((kwargs["reply_to_message_id"], kwargs["response_text"]))
        return SimpleNamespace(queued=len(observed) == 1, deduped=False, reason_code="user_capacity")

    chat = make_chat_state()
    chat.voice_id = "Aoede"
    chat.tts_temperature = 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    monkeypatch.setattr("app.handlers.msg_speech.metrics_collector", SimpleNamespace(record_request=AsyncMock()))
    update = SimpleNamespace(
        effective_message=command, effective_user=SimpleNamespace(id=11), effective_chat=command.chat
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await handle_speech_request(update, SimpleNamespace(bot=bot), request)
    assert observed == [(101, "Один"), (102, "Один"), (103, "Три")]
    assert [call.kwargs["reply_to_message_id"] for call in bot.send_message.await_args_list] == [102, 103]


@pytest.mark.asyncio
async def test_deleted_source_notification_failure_does_not_abort_later_source_admissions(monkeypatch):
    from telegram.error import BadRequest

    from tests.factories import make_chat_state

    command = message("Озвучь эти сообщения", 110)
    sources = [
        message("Один", 101, forwarded=True),
        message("Два", 102, forwarded=True),
        message("Три", 103, forwarded=True),
    ]
    request = speech_request_for_message(command, DebounceResult([*(entry(item) for item in sources), entry(command)]))
    observed, notices = [], []

    async def enqueue(**kwargs):
        observed.append(kwargs["reply_to_message_id"])
        return SimpleNamespace(queued=False, deduped=False, reason_code="user_capacity")

    async def send(**kwargs):
        if kwargs.get("reply_to_message_id") == 101:
            raise BadRequest("Message to be replied not found")
        notices.append(kwargs)

    chat = make_chat_state()
    chat.voice_id, chat.tts_temperature = "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    bot = SimpleNamespace(send_message=send)
    update = SimpleNamespace(
        effective_message=command, effective_user=SimpleNamespace(id=11), effective_chat=command.chat
    )
    await handle_speech_request(update, SimpleNamespace(bot=bot), request)
    assert observed == [101, 102, 103]
    assert [item.get("reply_to_message_id") for item in notices] == [None, 102, 103]

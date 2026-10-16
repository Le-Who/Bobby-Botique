"""Voice buttons respect text admission before its canonical lock is acquired."""

import asyncio
from contextlib import asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Message, Update, User
from telegram.ext import Application, ExtBot

from app import state
from app.handlers import callbacks, cb_voice, messages
from app.i18n import t
from app.runtime_settings.store import SettingsSnapshot
from app.update_processor import UserScopedUpdateProcessor
from tests import test_edited_request_ownership as support

pytestmark = pytest.mark.asyncio
ownership = support.ownership
ACTIONS = ["confirm", "deep_search", "transcribe_only", "retranscribe_flash", "edit"]


@pytest.fixture
async def admission(monkeypatch, ownership):
    """Keep PTB dispatch, request ownership, canonical locks and TaskManager real."""
    processor = UserScopedUpdateProcessor(50)
    application = (
        Application.builder().token("1234567890:synthetic-admission-token").concurrent_updates(processor).build()
    )
    # Lifecycle startup contacts Telegram. Only offline dispatch needs these flags.
    application._initialized = True
    application._running = True
    application.bot._bot_user = User(999, "Synthetic", True, username="synthetic_bot")
    messages.register(application)
    callbacks.register(application)
    handlers = [handler for group in application.handlers.values() for handler in group]
    text_handlers = [handler for handler in handlers if handler.callback is messages.handle_request]
    voice_handlers = [handler for handler in handlers if handler.callback is cb_voice.voice_callback]
    assert text_handlers and all(handler.block is False for handler in text_handlers)
    assert len(voice_handlers) == 1 and voice_handlers[0].block

    errors = []

    async def error_handler(_update, context):
        errors.append(context.error)

    application.add_error_handler(error_handler)
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr(
        "app.runtime_settings.store.get_snapshot", AsyncMock(return_value=SettingsSnapshot(0, MappingProxyType({})))
    )
    monkeypatch.setattr(cb_voice, "_background_tasks", set())
    monkeypatch.setattr(cb_voice, "_voice_operations", {})
    monkeypatch.setattr(cb_voice, "_HEAVY_CALLBACK_SEMAPHORE", asyncio.Semaphore(4))
    current = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", current)
    effects = []
    chats = {uid: SimpleNamespace(history=[], memory_epoch=52) for uid in (11, 22)}

    async def get_chat(uid):
        effects.append(("chat", uid))
        return chats[uid]

    @asynccontextmanager
    async def lease(uid, epoch, *, purpose, require_ltm):
        assert epoch == 52 and require_ltm is False
        effects.append(("lease", uid))
        yield True

    def voice_work(action):
        async def work(_message, uid, *_args, **_kwargs):
            assert state.get_user_lock(uid).locked()
            effects.append((action, uid))

        return work

    async def asr(data, *, model):
        assert data == b"synthetic-audio" and model == "gemini-3.5-flash"
        effects.append(("asr", None))
        return "Updated synthetic transcript", "conversational", None

    async def transcript(_message, text, lang, uid, *_args, **_kwargs):
        assert text == "Synthetic transcript" and lang == "ru"
        assert state.get_user_lock(uid).locked()
        effects.append(("transcribe_only", uid))

    monkeypatch.setattr(cb_voice, "get_user_chat", get_chat)
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr("app.handlers.ai_chat._handle_regular_chat", voice_work("confirm"))
    monkeypatch.setattr("app.handlers.ai_search._handle_research_agent", voice_work("deep_search"))
    monkeypatch.setattr("app.handlers.msg_voice._show_transcript_only", transcript)
    monkeypatch.setattr("app.handlers.msg_voice._show_confirmation_ui", AsyncMock())
    monkeypatch.setattr("app.utils.multimodal_processor.transcribe_voice", asr)
    entered, release, worker_entered, release_worker = (asyncio.Event() for _ in range(4))
    other_entered, release_other, other_cleaned = (asyncio.Event() for _ in range(3))
    window = {"name": None}
    text_effects = []

    async def intent(text):
        if window["name"] == "intent" and text.endswith("11"):
            entered.set()
            await release.wait()
        return None

    async def text_work(_placeholder, update, _context, **_kwargs):
        assert state.get_user_lock(update.effective_user.id).locked()
        text_effects.append(update.effective_user.id)
        if update.effective_user.id == 22:
            other_entered.set()
            try:
                await release_other.wait()
            finally:
                other_cleaned.set()
            return
        worker_entered.set()
        await release_worker.wait()

    monkeypatch.setattr("app.intent_router.try_direct_intent", intent)
    monkeypatch.setattr("app.handlers.agent.process_long_request", text_work)
    sent, edits, answers = [], [], []
    message_id = 1000

    async def send_message(_bot, chat_id, text, **_kwargs):
        nonlocal message_id
        if window["name"] == "placeholder" and chat_id == 11 and text == t("msg.thinking"):
            entered.set()
            await release.wait()
        message_id += 1
        sent.append((chat_id, text))
        return Message.de_json(
            {"message_id": message_id, "date": 1, "chat": {"id": chat_id, "type": "private"}, "text": text},
            application.bot,
        )

    async def edit_message(_bot, **kwargs):
        edits.append(kwargs)
        return True

    async def answer_callback(_bot, **kwargs):
        answers.append(kwargs)
        return True

    monkeypatch.setattr(ExtBot, "send_message", send_message)
    monkeypatch.setattr(ExtBot, "edit_message_text", edit_message)
    monkeypatch.setattr(ExtBot, "answer_callback_query", answer_callback)
    monkeypatch.setattr(ExtBot, "send_chat_action", AsyncMock(return_value=True))
    sequence = 0

    def update(uid, *, action=None):
        nonlocal sequence
        sequence += 1
        actor = {"id": uid, "is_bot": False, "first_name": "Synthetic"}
        message = {"message_id": 900 if action else sequence, "date": 1, "chat": {"id": uid, "type": "private"}}
        if action:
            message["text"] = "Synthetic voice confirmation"
            payload = {
                "callback_query": {
                    "id": f"voice-{sequence}",
                    "chat_instance": "synthetic",
                    "data": f"voice:{action}",
                    "from": actor,
                    "message": message,
                }
            }
        else:
            message.update({"from": actor, "text": f"Synthetic text request {uid}"})
            payload = {"message": message}
        return Update.de_json({"update_id": sequence, **payload}, application.bot)

    async def dispatch(item):
        await processor.process_update(item, application.process_update(item))
        assert processor._user_locks == {}
        assert errors == []

    def pending(uid=11):
        item = {
            "user_id": uid,
            "placeholder_id": 900,
            "memory_epoch": 52,
            "lang": "ru",
            "transcript": "Synthetic transcript",
            "voice_bytes": b"synthetic-audio",
            "file_unique_id": "synthetic-voice",
        }
        application.user_data[uid]["voice_pending_900"] = item
        return item

    async def start_text(name):
        window["name"] = name
        await dispatch(update(11))
        # Real block=False dispatch has returned; its handler still owns admission.
        await entered.wait()
        owner = state.get_active_task(11)
        assert owner is not None and not owner.done()
        assert state.get_user_state(11).is_processing
        assert not state.get_user_lock(11).locked()
        return owner

    async def finish_text(owner, terminal):
        if terminal == "cancel":
            assert state.cancel_active_task(11)
            await asyncio.gather(owner, return_exceptions=True)
        else:
            release.set()
            await owner
            await worker_entered.wait()
            worker = state.get_active_task(11)
            assert worker is not None and worker is not owner
            release_worker.set()
            await worker
        await support.checkpoint()
        assert state.get_active_task(11) is None
        assert not state.get_user_state(11).is_processing
        assert not state.get_user_lock(11).locked()

    async def drain_voice(item):
        task = item.get("_task")
        if task is not None:
            await task
        await support.checkpoint()
        assert cb_voice._voice_operations == {}
        assert cb_voice._background_tasks == set()

    try:
        yield SimpleNamespace(
            application=application,
            dispatch=dispatch,
            update=update,
            pending=pending,
            start_text=start_text,
            finish_text=finish_text,
            drain_voice=drain_voice,
            sent=sent,
            edits=edits,
            answers=answers,
            effects=effects,
            chats=chats,
            current=current,
            text_effects=text_effects,
            other_entered=other_entered,
            release_other=release_other,
            other_cleaned=other_cleaned,
        )
    finally:
        release.set()
        release_worker.set()
        release_other.set()
        tasks = set(application._Application__create_task_tasks) | cb_voice._background_tasks | set(ownership.workers)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        application._running = False
        application._initialized = False
        assert errors == []


def assert_text_reserved(owner):
    assert state.get_active_task(11) is owner and not owner.done()
    assert state.get_user_state(11).is_processing
    assert not state.get_user_lock(11).locked()


async def reject_busy(case, item, action):
    snapshot = dict(item)
    answers_before = len(case.answers)
    sent_before, edits_before = list(case.sent), list(case.edits)
    await case.dispatch(case.update(11, action=action))
    assert [answer.get("text") for answer in case.answers[answers_before:]] == [t("busy.toast", "ru")], (
        "voice callback must reject a pre-lock text reservation"
    )
    assert case.application.user_data[11]["voice_pending_900"] is item
    assert item == snapshot
    assert cb_voice._voice_operations == {} and cb_voice._background_tasks == set()
    assert case.effects == []
    case.current.assert_not_awaited()
    assert case.chats[11].history == []
    assert "voice_edit_pending" not in case.application.user_data[11]
    assert case.sent == sent_before and case.edits == edits_before


@pytest.mark.parametrize("window", ["intent", "placeholder"])
@pytest.mark.parametrize("action", ACTIONS)
async def test_text_reservation_rejects_voice_without_consuming_pending(admission, window, action):
    item = admission.pending()
    owner = await admission.start_text(window)
    await reject_busy(admission, item, action)
    assert_text_reserved(owner)
    await admission.finish_text(owner, "complete")
    assert admission.text_effects == [11]


@pytest.mark.parametrize("action", ACTIONS)
async def test_idle_voice_action_still_admits_once(admission, action):
    item = admission.pending()
    await admission.dispatch(admission.update(11, action=action))
    await admission.drain_voice(item)
    assert [answer.get("text") for answer in admission.answers] == [None]
    assert not state.get_user_lock(11).locked()
    assert not state.get_user_state(11).is_processing
    expected_effect = "asr" if action == "retranscribe_flash" else action
    if action == "edit":
        assert admission.application.user_data[11]["voice_edit_pending"] is True
    else:
        assert sum(effect[0] == expected_effect for effect in admission.effects) == 1
    assert admission.edits or action == "transcribe_only"
    assert len(admission.chats[11].history) == (1 if action == "deep_search" else 0)


@pytest.mark.parametrize("action", ACTIONS)
async def test_held_canonical_lock_still_rejects_voice(admission, action):
    item = admission.pending()
    lock = state.get_user_lock(11)
    await lock.acquire()
    try:
        await reject_busy(admission, item, action)
        assert lock.locked()
        assert not state.get_user_state(11).is_processing
    finally:
        lock.release()


@pytest.mark.parametrize("window", ["intent", "placeholder"])
@pytest.mark.parametrize("terminal", ["complete", "cancel"])
async def test_preserved_pending_retries_after_text_releases_admission(admission, window, terminal):
    item = admission.pending()
    owner = await admission.start_text(window)
    await reject_busy(admission, item, "confirm")
    assert_text_reserved(owner)
    await admission.finish_text(owner, terminal)
    assert admission.application.user_data[11]["voice_pending_900"] is item
    await admission.dispatch(admission.update(11, action="confirm"))
    await admission.drain_voice(item)
    assert admission.effects.count(("confirm", 11)) == 1
    assert "voice_pending_900" not in admission.application.user_data[11]
    assert not state.get_user_lock(11).locked()
    assert admission.text_effects == ([11] if terminal == "complete" else [])


@pytest.mark.parametrize("window", ["intent", "placeholder"])
async def test_other_user_voice_runs_while_text_reservation_survives(admission, window):
    owner = await admission.start_text(window)
    item = admission.pending(22)
    await admission.dispatch(admission.update(22, action="confirm"))
    await admission.drain_voice(item)
    assert admission.effects.count(("confirm", 22)) == 1
    assert not state.get_user_lock(22).locked()
    assert_text_reserved(owner)
    await admission.finish_text(owner, "complete")


@pytest.mark.parametrize("window", ["intent", "placeholder"])
async def test_voice_cancel_remains_available_without_releasing_text_owner(admission, window):
    admission.pending()
    other = admission.pending(22)
    owner = await admission.start_text(window)
    await admission.dispatch(admission.update(11, action="cancel"))
    assert "voice_pending_900" not in admission.application.user_data[11]
    assert admission.application.user_data[22]["voice_pending_900"] is other
    assert [answer.get("text") for answer in admission.answers] == [None]
    assert admission.edits[-1]["text"] == t("voice.cancelled", "ru")
    assert cb_voice._voice_operations == {} and cb_voice._background_tasks == set()
    assert admission.effects == []
    assert_text_reserved(owner)
    await admission.finish_text(owner, "complete")


async def test_cancelling_text_and_voice_pending_does_not_disturb_another_text_owner(admission):
    await admission.dispatch(admission.update(22))
    await admission.other_entered.wait()
    other_owner = state.get_active_task(22)
    assert other_owner is not None and not other_owner.done()
    item = admission.pending()
    owner = await admission.start_text("intent")
    await reject_busy(admission, item, "confirm")
    await admission.dispatch(admission.update(11, action="cancel"))
    assert_text_reserved(owner)
    await admission.finish_text(owner, "cancel")
    assert state.get_active_task(22) is other_owner and not other_owner.done()
    assert state.get_user_state(22).is_processing and state.get_user_lock(22).locked()
    assert not admission.other_cleaned.is_set()
    admission.release_other.set()
    await other_owner
    await support.checkpoint()
    assert admission.other_cleaned.is_set()
    assert state.get_active_task(22) is None
    assert not state.get_user_state(22).is_processing
    assert not state.get_user_lock(22).locked()

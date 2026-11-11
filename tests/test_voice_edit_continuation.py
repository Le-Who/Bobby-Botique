"""Edited transcripts retire only their selected voice source generation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from app import state
from app.handlers import cb_voice, commands, messages
from app.handlers.msg_voice import _show_confirmation_ui
from app.voice_intent import SpeechRequestIntent, SpeechSourcePart
from tests import test_edited_request_ownership as text_support
from tests import test_voice_callback_admission as voice_support

pytestmark = pytest.mark.asyncio
ownership = text_support.ownership
admission = voice_support.admission


def callback(case, action, message_id=900):
    payload = case.update(11, action=action).to_dict()
    payload["callback_query"]["message"]["message_id"] = message_id
    return Update.de_json(payload, case.application.bot)


async def send_text(case, monkeypatch, *, chat_id=11):
    work = AsyncMock()
    monkeypatch.setattr("app.handlers.agent.process_long_request", work)
    payload = case.update(11).to_dict()
    payload["message"]["chat"]["id"] = chat_id
    await case.dispatch(Update.de_json(payload, case.application.bot))
    await text_support.settle()
    work.assert_awaited_once()
    assert work.call_args.args[1].effective_message.text == "Synthetic text request 11"


async def test_corrected_text_consumes_source_and_blocks_original_confirm(admission, monkeypatch):
    item = admission.pending()
    await admission.dispatch(callback(admission, "edit"))
    await send_text(admission, monkeypatch)

    assert "voice_pending_900" not in admission.application.user_data[11]
    assert "voice_edit_pending" not in admission.application.user_data[11]
    await admission.dispatch(callback(admission, "confirm"))
    await admission.drain_voice(item)
    assert ("confirm", 11) not in admission.effects


async def test_new_edit_supersedes_old_ui_without_old_cancel_clearing_it(admission, monkeypatch):
    original = admission.pending()
    replacement = dict(original, placeholder_id=901)
    data = admission.application.user_data[11]
    data["voice_pending_901"] = replacement
    await admission.dispatch(callback(admission, "edit"))
    await admission.dispatch(callback(admission, "edit", 901))
    await admission.dispatch(callback(admission, "cancel"))
    await admission.dispatch(callback(admission, "edit"))

    await send_text(admission, monkeypatch)
    assert "voice_pending_900" not in data
    assert "voice_pending_901" not in data
    assert "voice_edit_pending" not in data


@pytest.mark.parametrize("terminal", ["cancel", "reset", "privacy"])
async def test_invalidated_edit_does_not_capture_later_text(admission, monkeypatch, terminal):
    admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    if terminal == "cancel":
        await admission.dispatch(callback(admission, "cancel"))
        assert "voice_edit_pending" not in data
    elif terminal == "reset":
        data.pop("voice_pending_900")
    else:
        admission.current.return_value = False

    await send_text(admission, monkeypatch)
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data


async def test_stale_epoch_check_cannot_consume_new_selection(admission, monkeypatch):
    original = admission.pending()
    replacement = dict(original, placeholder_id=901)
    data = admission.application.user_data[11]
    data["voice_pending_901"] = replacement
    await admission.dispatch(callback(admission, "edit"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def current(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return False

    admission.current.side_effect = current
    text = asyncio.create_task(cb_voice.prepare_voice_edit(admission.application.user_data[11], 11, 11))
    try:
        await entered.wait()
        admission.current.side_effect = None
        await admission.dispatch(callback(admission, "edit", 901))
        selection = data["voice_edit_pending"]
        release.set()
        await text
        assert data["voice_edit_pending"] is selection
        assert data["voice_pending_901"] is replacement
    finally:
        release.set()
        await asyncio.gather(text, return_exceptions=True)

    await send_text(admission, monkeypatch)
    assert "voice_pending_901" not in data


async def test_new_confirmation_retires_previous_edit_ui(admission):
    admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    await _show_confirmation_ui(
        SimpleNamespace(message_id=901, chat=SimpleNamespace(id=11), edit_text=AsyncMock()),
        "New synthetic transcript",
        "ru",
        11,
        b"synthetic-new-audio",
        SimpleNamespace(file_unique_id="new-synthetic-voice"),
        SimpleNamespace(user_data=data),
        memory_epoch=52,
    )

    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data
    replacement = data["voice_pending_901"]
    await admission.dispatch(callback(admission, "edit"))
    assert "voice_edit_pending" not in data
    assert data["voice_pending_901"] is replacement


async def test_busy_text_keeps_selected_source_for_retry(admission, monkeypatch):
    item = admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    selection = data["voice_edit_pending"]
    work = AsyncMock()
    monkeypatch.setattr("app.handlers.agent.process_long_request", work)
    async with state.get_user_lock(11):
        await admission.dispatch(admission.update(11))
        await text_support.settle()
        work.assert_not_awaited()
        assert data["voice_pending_900"] is item
        assert data["voice_edit_pending"] is selection

    await send_text(admission, monkeypatch)
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data


async def test_other_chat_cannot_select_same_message_number(admission):
    item = admission.pending()
    item["chat_id"] = 12
    await admission.dispatch(callback(admission, "edit"))
    assert "voice_edit_pending" not in admission.application.user_data[11]
    assert admission.application.user_data[11]["voice_pending_900"] is item


async def test_replaced_generation_survives_old_text_selection(admission, monkeypatch):
    original = admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    replacement = dict(original, transcript="New source", _edit_token=object())
    data["voice_pending_900"] = replacement
    await send_text(admission, monkeypatch)

    assert "voice_edit_pending" not in data
    assert data["voice_pending_900"] is replacement


async def test_text_in_other_chat_preserves_edit_selection(admission, monkeypatch):
    item = admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    selection = data["voice_edit_pending"]
    await send_text(admission, monkeypatch, chat_id=12)
    assert data["voice_edit_pending"] is selection
    assert data["voice_pending_900"] is item

    await send_text(admission, monkeypatch)
    assert "voice_edit_pending" not in data
    assert "voice_pending_900" not in data


async def test_new_chat_clears_voice_edit_and_sources(admission, monkeypatch):
    admission.pending()
    data = admission.application.user_data[11]
    data["unrelated"] = "preserved"
    await admission.dispatch(callback(admission, "edit"))
    monkeypatch.setattr(commands, "get_user_chat", AsyncMock(return_value=admission.chats[11]))
    monkeypatch.setattr(commands, "update_user_chat", AsyncMock())
    monkeypatch.setattr(commands, "leave_tarot_for_chat", AsyncMock())
    await commands.new_chat_command.__wrapped__(
        SimpleNamespace(effective_user=SimpleNamespace(id=11), message=SimpleNamespace(reply_text=AsyncMock())),
        SimpleNamespace(user_data=data),
    )
    assert data == {"unrelated": "preserved"}


@pytest.mark.parametrize("terminal", ["busy", "cancel", "capacity", "draw_capacity"])
async def test_pre_admission_interruption_preserves_corrected_source(admission, monkeypatch, terminal):
    item = admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    selection = data["voice_edit_pending"]
    entered, release = asyncio.Event(), asyncio.Event()
    owner = None
    work = AsyncMock()
    monkeypatch.setattr("app.handlers.agent.process_long_request", work)

    async def debounce(_uid, message, **_kwargs):
        nonlocal owner
        owner = asyncio.current_task()
        entered.set()
        await release.wait()
        return SimpleNamespace(
            build_llm_context=lambda: message.text,
            has_forwarded_content=False,
            forwarded_photo_messages=[],
            user_entries=[],
            forwarded_entries=[],
        )

    monkeypatch.setattr("app.middleware.debounce.debounce_message", debounce)
    submit = messages.try_submit_task

    def reject(coro, **_kwargs):
        coro.close()
        return None

    await admission.dispatch(admission.update(11))
    await entered.wait()
    try:
        if terminal == "busy":
            async with state.get_user_lock(11):
                release.set()
                await text_support.settle()
                messages._send_busy_ephemeral.assert_awaited_once()
        elif terminal == "cancel":
            assert owner is not None
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        else:
            if terminal == "draw_capacity":
                monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", AsyncMock(return_value="Draw"))
            monkeypatch.setattr(messages, "try_submit_task", reject)
            release.set()
            await text_support.settle()
        work.assert_not_awaited()
        assert data.get("voice_pending_900") is item
        assert data.get("voice_edit_pending") is selection
    finally:
        release.set()
        if owner is not None:
            await asyncio.gather(owner, return_exceptions=True)

    monkeypatch.setattr(messages, "try_submit_task", submit)
    monkeypatch.setattr("app.handlers.cmd_image.check_draw_intent_async", AsyncMock(return_value=None))
    await send_text(admission, monkeypatch)
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data


async def test_admitted_text_cannot_commit_newer_edit_selection(admission, monkeypatch):
    original = admission.pending()
    data = admission.application.user_data[11]
    replacement = dict(original, placeholder_id=901)
    data["voice_pending_901"] = replacement
    await admission.dispatch(callback(admission, "edit"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def debounce(_uid, message, **_kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(
            build_llm_context=lambda: message.text,
            has_forwarded_content=False,
            forwarded_photo_messages=[],
            user_entries=[],
            forwarded_entries=[],
        )

    work = AsyncMock()
    monkeypatch.setattr("app.handlers.agent.process_long_request", work)
    monkeypatch.setattr("app.middleware.debounce.debounce_message", debounce)
    await admission.dispatch(admission.update(11))
    await entered.wait()
    try:
        await admission.dispatch(callback(admission, "edit", 901))
        selection = data["voice_edit_pending"]
        release.set()
        await text_support.settle()
        work.assert_awaited_once()
        assert data["voice_edit_pending"] is selection
        assert data["voice_pending_901"] is replacement
    finally:
        release.set()

    await send_text(admission, monkeypatch)
    assert "voice_pending_901" not in data
    assert "voice_edit_pending" not in data


@pytest.mark.parametrize("route", ["direct_intent", "role_input"])
async def test_consumed_specialized_text_retires_source(admission, monkeypatch, route):
    admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    work = AsyncMock()
    monkeypatch.setattr("app.handlers.agent.process_long_request", work)
    if route == "direct_intent":
        monkeypatch.setattr(
            "app.intent_router.try_direct_intent",
            AsyncMock(return_value=SimpleNamespace(handled=True, text="Synthetic intent response")),
        )
    else:
        monkeypatch.setattr(messages, "handle_role_rename", AsyncMock(return_value=True))
    await admission.dispatch(admission.update(11))
    await text_support.settle()
    work.assert_not_awaited()
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("terminal", ["capacity", "user_capacity", "stale_epoch", "queued", "cancel"])
async def test_speech_edit_follows_queue_admission(admission, monkeypatch, collection, terminal):
    item = admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    selection = data["voice_edit_pending"]
    chat = admission.chats[11]
    chat.voice_id, chat.tts_temperature = "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    entered = asyncio.Event()
    owner = None

    async def enqueue(**_kwargs):
        nonlocal owner
        owner = asyncio.current_task()
        entered.set()
        if terminal == "cancel":
            await asyncio.Future()
        return SimpleNamespace(queued=terminal == "queued", deduped=False, reason_code=terminal)

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    payload = admission.update(11).to_dict()
    payload["message"]["text"] = "Озвучь: " + ("а " * 2044 if collection else "Synthetic corrected text")
    await admission.dispatch(Update.de_json(payload, admission.application.bot))
    await entered.wait()
    if terminal == "cancel":
        assert owner is not None
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
    else:
        await text_support.settle()
    if terminal == "queued":
        assert "voice_pending_900" not in data
        assert "voice_edit_pending" not in data
    else:
        assert data.get("voice_pending_900") is item
        assert data.get("voice_edit_pending") is selection


async def test_partial_speech_admission_then_cancel_retires_selected_source(admission, monkeypatch):
    admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    chat = admission.chats[11]
    chat.voice_id, chat.tts_temperature = "Aoede", 0.3
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=chat))
    request = SpeechRequestIntent(
        "speak", "First second", "inline", (SpeechSourcePart("First", 501), SpeechSourcePart("Second", 502))
    )
    monkeypatch.setattr("app.handlers.msg_speech.speech_request_for_message", lambda *_args: request)
    entered = asyncio.Event()
    owner = None
    calls = 0

    async def enqueue(**_kwargs):
        nonlocal owner, calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(queued=True, deduped=False)
        owner = asyncio.current_task()
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    await admission.dispatch(admission.update(11))
    await entered.wait()
    assert owner is not None
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data


async def test_speech_clarification_consumes_selected_source(admission, monkeypatch):
    admission.pending()
    data = admission.application.user_data[11]
    await admission.dispatch(callback(admission, "edit"))
    enqueue = AsyncMock()
    monkeypatch.setattr("app.voice_engine.fire_voice_reply", enqueue)
    payload = admission.update(11).to_dict()
    payload["message"]["text"] = "Озвучь"
    await admission.dispatch(Update.de_json(payload, admission.application.bot))
    await text_support.settle()
    enqueue.assert_not_awaited()
    assert "voice_pending_900" not in data
    assert "voice_edit_pending" not in data

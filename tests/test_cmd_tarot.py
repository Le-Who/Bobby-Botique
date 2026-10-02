"""Offline regression coverage for the common Tarot entry point."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from app import state
from app.handlers import cmd_tarot
from app.repos import users

_USER_ID = 98764001


def _persisted_fields():
    return {
        "document_mode": True,
        "selected_document_id": 42,
        "awaiting_custom_role_input": True,
        "generated_role": {"title": "Saved role", "prompt": "Synthetic prompt"},
        "last_custom_role_prompt": "Synthetic previous prompt",
        "generating_custom_role": True,
        "last_sent_message_text": "Synthetic previous response",
        "awaiting_manual_role_title": False,
        "awaiting_manual_role_prompt": True,
        "manual_role_title": "Saved manual role",
        "manual_role_prompt": "Synthetic manual prompt",
        "role_diaries": {"7": ["Synthetic diary entry"]},
        "tarot_mode": False,
        "tarot_session": None,
    }


def _update(entry="command"):
    user = SimpleNamespace(id=_USER_ID, language_code="ru", first_name="Test", username="test")
    chat = SimpleNamespace(id=_USER_ID, type="private")
    message = SimpleNamespace(text="/tarot", reply_text=AsyncMock(), chat=chat, from_user=user, message_id=1)
    callback = (
        SimpleNamespace(id="tarot-entry", data="start_tarot", answer=AsyncMock(), message=message)
        if entry == "callback"
        else None
    )
    return SimpleNamespace(
        effective_user=user,
        effective_chat=chat,
        effective_message=message,
        message=None if callback else message,
        callback_query=callback,
        update_id=123,
    )


def _context():
    return SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()), application=SimpleNamespace())


@pytest.fixture(autouse=True)
def _offline_repositories(monkeypatch):
    load = AsyncMock(return_value=_persisted_fields())
    save = AsyncMock()
    monkeypatch.setattr(users, "load_user_state", load)
    monkeypatch.setattr(users, "save_user_state", save)
    monkeypatch.setattr(users, "is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    # Keep real scheduling/persistence, but flush it deterministically below.
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 3600)
    yield SimpleNamespace(load=load, save=save)
    handle = state._pending_persists.pop(_USER_ID, None)
    if handle is not None:
        handle.cancel()


async def _flush_state():
    handle = state._pending_persists.pop(_USER_ID)
    handle.cancel()
    await state._persist(state.get_user_state(_USER_ID))


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "callback"])
async def test_cold_tarot_session_survives_hydration_by_the_next_handler(entry, _offline_repositories):
    context = _context()

    result = await cmd_tarot.tarot_command(_update(entry), context)
    session = state.get_tarot_session(_USER_ID)
    await state.ensure_state_loaded(_USER_ID)

    assert state.is_in_tarot_mode(_USER_ID)
    assert state.get_tarot_session(_USER_ID) is session
    assert session["waiting_for_question"] is True
    assert session["drawn_cards"] == []
    assert result is True
    _offline_repositories.load.assert_awaited_once_with(_USER_ID)


@pytest.mark.asyncio
async def test_tarot_hydrates_before_mutating_state_or_clearing_pending_input(_offline_repositories):
    context = _context()
    context.user_data["compatibility_flow"] = {"first_date": "Synthetic previous input"}
    observations = []

    async def load(user_id):
        current = state.get_user_state(user_id)
        observations.append((current.tarot_mode, current.tarot_session, "compatibility_flow" in context.user_data))
        return _persisted_fields()

    _offline_repositories.load.side_effect = load

    await cmd_tarot.tarot_command(_update(), context)

    assert observations == [(False, None, True)]
    assert "compatibility_flow" not in context.user_data
    assert state.is_in_tarot_mode(_USER_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "draw_button"),
    [
        ("Что поможет этой паре лучше понять друг друга?", "🔮 Сделать расклад на совместимость"),
        ("What could help this pair understand each other?", "🔮 Read tarot for this pair"),
    ],
    ids=["ru", "en"],
)
async def test_tarot_persistence_preserves_loaded_fields_and_question_context(
    question, draw_button, _offline_repositories
):
    context = _context()

    result = await cmd_tarot.tarot_command(_update(), context, question_context=question)
    await _flush_state()

    expected = {
        "user_id": _USER_ID,
        **_persisted_fields(),
        "tarot_mode": True,
        "tarot_session": {
            "spread_type": "tarot",
            "drawn_cards": [],
            "history": [],
            "waiting_for_question": True,
            "last_activity_at": ANY,
            "question_context": question,
        },
    }
    _offline_repositories.save.assert_awaited_once_with(**expected)
    buttons = context.bot.send_message.await_args.kwargs["reply_markup"].keyboard
    assert buttons[0][0].text == draw_button
    assert buttons[1][0].text == "🛑 Завершить сеанс Таро"
    assert result is True


@pytest.mark.asyncio
async def test_warm_tarot_entry_keeps_current_fields_without_reloading(_offline_repositories):
    current = state.get_user_state(_USER_ID)
    current._loaded_from_db = True
    current.document_mode = True
    current.selected_document_id = 99
    current.role_diaries = {"8": ["Synthetic current entry"]}
    current.tarot_mode = True
    previous_session = {"history": ["Synthetic previous reading"]}
    current.tarot_session = previous_session

    result = await cmd_tarot.tarot_command(_update(), _context())

    assert current.document_mode is True
    assert current.selected_document_id == 99
    assert current.role_diaries == {"8": ["Synthetic current entry"]}
    assert current.tarot_mode is True
    assert current.tarot_session is not previous_session
    assert current.tarot_session["waiting_for_question"] is True
    assert result is True
    _offline_repositories.load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("load_failure", [False, True], ids=["no-saved-state", "repository-error"])
async def test_tarot_entry_keeps_existing_hydration_failure_contract(load_failure, _offline_repositories):
    _offline_repositories.load.return_value = None
    if load_failure:
        _offline_repositories.load.side_effect = RuntimeError("Synthetic repository unavailable")

    result = await cmd_tarot.tarot_command(_update(), _context())
    current = state.get_user_state(_USER_ID)

    assert current._loaded_from_db is True
    assert current.tarot_mode is True
    assert current.tarot_session["waiting_for_question"] is True
    assert result is True
    assert await state.ensure_state_loaded(_USER_ID) is current
    _offline_repositories.load.assert_awaited_once_with(_USER_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "callback"])
@pytest.mark.parametrize("previously_active", [False, True], ids=["inactive", "active"])
async def test_failed_tarot_welcome_preserves_loaded_session_and_pending_input(
    entry, previously_active, _offline_repositories
):
    update = _update(entry)
    context = _context()
    previous_session = {"history": ["Synthetic previous reading"]} if previously_active else None
    _offline_repositories.load.return_value.update(tarot_mode=previously_active, tarot_session=previous_session)
    flow = {"first_date": "Synthetic previous input"}
    timer = asyncio.get_running_loop().call_later(3600, lambda: None)
    context.user_data.update(compatibility_flow=flow, compatibility_expiry_timer=timer)
    context.bot.send_message.side_effect = RuntimeError("Synthetic Telegram delivery failure")

    try:
        result = await cmd_tarot.tarot_command(update, context)

        assert result is None
        current = state.get_user_state(_USER_ID)
        assert current._loaded_from_db is True
        assert current.tarot_mode is previously_active
        assert current.tarot_session is previous_session
        assert current.selected_document_id == 42
        assert context.user_data["compatibility_flow"] is flow
        assert flow["first_date"] == "Synthetic previous input"
        assert context.user_data["compatibility_expiry_timer"] is timer
        assert not timer.cancelled()
        assert _USER_ID not in state._pending_persists
        _offline_repositories.save.assert_not_awaited()
        if entry == "command":
            update.message.reply_text.assert_awaited_once_with("Произошла ошибка при запуске режима Таро")
        else:
            update.callback_query.answer.assert_awaited_once_with(
                "Произошла ошибка при запуске режима Таро", show_alert=True
            )
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_failed_regular_tarot_welcome_preserves_real_natal_transition(_offline_repositories):
    from app.handlers import natal_chart

    update = _update("callback")
    context = _context()
    token = "ab" * 8
    flow = {"first_date": "Synthetic previous input"}
    stored = {"question": "Synthetic pair question", "lang": "en", "created_at": time.monotonic()}
    context.user_data.update(
        natal_date="Synthetic natal selection",
        compatibility_flow=flow,
        compatibility_tarot_context={token: stored},
    )
    context.bot.send_message.side_effect = RuntimeError("Synthetic Telegram delivery failure")

    result = await natal_chart.on_tarot_during_natal(update, context)

    assert result is None
    assert not state.is_in_tarot_mode(_USER_ID)
    assert state.get_tarot_session(_USER_ID) is None
    assert context.user_data["natal_date"] == "Synthetic natal selection"
    assert context.user_data["compatibility_flow"] is flow
    assert flow["first_date"] == "Synthetic previous input"
    assert context.user_data["compatibility_tarot_context"][token] is stored
    assert _USER_ID not in state._pending_persists
    _offline_repositories.save.assert_not_awaited()

"""Provider completions belong to the input/session that admitted them."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import state
from app.handlers import cb_roles, msg_roles, tarot_chat


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_schedule_persist", lambda _state: None)
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(return_value=None))
    monkeypatch.setattr("app.utils.decorators.is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.memory_consent.resolve_current_epoch", AsyncMock(return_value=7))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=7))
    monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=SimpleNamespace(memory_epoch=7)))
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", AsyncMock(return_value=True))

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    monkeypatch.setattr(msg_roles, "get_user_chat", AsyncMock(return_value=SimpleNamespace(model="test-model")))
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=SimpleNamespace(model="test-model")))
    monkeypatch.setattr("app.process_policies.resolve_process", AsyncMock(return_value=SimpleNamespace(explicit=True)))


def update(text="Synthetic question", user_id=912):
    progress = SimpleNamespace(message_id=81, edit_text=AsyncMock(), delete=AsyncMock())
    user = SimpleNamespace(id=user_id, language_code="ru")
    chat = SimpleNamespace(id=user_id, type="private")
    message = SimpleNamespace(text=text, from_user=user, chat=chat, reply_text=AsyncMock(return_value=progress))
    return SimpleNamespace(
        message=message, effective_message=message, effective_user=user, effective_chat=chat, callback_query=None
    ), progress


def held_provider(started, release, result):
    async def generate(*args, **kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Also fence SDKs which finish despite cancellation.
            await release.wait()
        return result, 1

    return generate


def callback(incoming, data):
    query = SimpleNamespace(
        id="synthetic-callback",
        from_user=incoming.effective_user,
        data=data,
        message=incoming.message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    incoming.callback_query = query
    return query


def tarot_session():
    state.set_tarot_mode(912, True)
    session = {"history": [], "drawn_cards": [], "last_activity_at": time.time()}
    state.set_tarot_session(912, session)
    return session


@pytest.mark.asyncio
async def test_cancelled_role_generation_cannot_restore_payload_or_preview(monkeypatch):
    incoming, progress = update()
    state.begin_custom_role_creation(912)
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        "app.process_policies.execute_text_process",
        held_provider(
            started, release, '{"title":"Old role","purpose":"Synthetic purpose","prompt":"Synthetic prompt"}'
        ),
    )
    task = asyncio.create_task(
        msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "Old")
    )
    await asyncio.wait_for(started.wait(), 2)
    state.clear_custom_role_state(912)
    release.set()
    await task

    assert state.get_generated_role(912) is None
    progress.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ended_tarot_turn_cannot_restore_session_or_deliver_answer(monkeypatch):
    incoming, _progress = update()
    state.set_tarot_mode(912, True)
    state.set_tarot_session(912, {"history": [], "drawn_cards": [], "last_activity_at": time.time()})
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tarot_chat, "get_provider_router", lambda: object())
    monkeypatch.setattr("app.process_policies.execute_text_process", held_provider(started, release, "Old answer"))
    task = asyncio.create_task(tarot_chat.handle_tarot_message(incoming, SimpleNamespace(user_data={})))
    await asyncio.wait_for(started.wait(), 2)
    state.clear_tarot_session(912)
    release.set()
    await task

    assert state.get_tarot_session(912) is None
    assert not state.is_in_tarot_mode(912)
    assert all(call.args[0] != "Old answer" for call in incoming.message.reply_text.await_args_list)


@pytest.mark.asyncio
async def test_old_generic_apply_cannot_use_a_new_owned_preview(monkeypatch):
    state.set_generated_role(912, {"title": "New", "prompt": "New prompt", "_interaction_token": "new-token"})
    incoming, _ = update()
    query = SimpleNamespace(
        from_user=incoming.effective_user, data="role_custom_apply", answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    incoming.callback_query = query
    writer = AsyncMock()
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)

    await cb_roles.role_custom_apply_callback(incoming, SimpleNamespace(user_data={}))

    writer.assert_not_awaited()
    assert state.get_generated_role(912)["title"] == "New"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["role", "tarot"])
async def test_competing_provider_input_is_visibly_rejected(monkeypatch, flow):
    first, _ = update("First")
    second, _ = update("Second")
    started, release = asyncio.Event(), asyncio.Event()
    result = '{"title":"Role","purpose":"Purpose","prompt":"Prompt"}' if flow == "role" else "Tarot answer"
    provider = AsyncMock(side_effect=held_provider(started, release, result))
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    monkeypatch.setattr(tarot_chat, "get_provider_router", lambda: object())
    context = SimpleNamespace(user_data={})
    if flow == "role":
        state.begin_custom_role_creation(912)
        first_call = msg_roles.handle_custom_role_generation(first, context, 912, 912, "First")
    else:
        tarot_session()
        first_call = tarot_chat.handle_tarot_message(first, context)
    task = asyncio.create_task(first_call)
    await asyncio.wait_for(started.wait(), 2)
    if flow == "role":
        assert await msg_roles.handle_custom_role_generation(second, context, 912, 912, "Second")
    else:
        await tarot_chat.handle_tarot_message(second, context)
    assert "Дождитесь" in second.message.reply_text.await_args.args[0]
    assert provider.await_count == 1
    release.set()
    await task
    assert not state.get_user_state(912)._interaction_tasks


@pytest.mark.asyncio
async def test_tarot_reset_replaces_snapshot_and_drains_old_turn(monkeypatch):
    original = tarot_session()
    incoming, _ = update()
    reset, _ = update("🔄 Новый расклад")
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(tarot_chat, "get_provider_router", lambda: object())
    monkeypatch.setattr("app.process_policies.execute_text_process", held_provider(started, release, "Old answer"))
    task = asyncio.create_task(tarot_chat.handle_tarot_message(incoming, SimpleNamespace(user_data={})))
    await asyncio.wait_for(started.wait(), 2)
    reset_task = asyncio.create_task(tarot_chat.handle_tarot_message(reset, SimpleNamespace(user_data={})))
    await asyncio.sleep(0)
    assert not reset_task.done()
    release.set()
    await asyncio.gather(task, reset_task)
    current = state.get_tarot_session(912)
    assert current is not original
    assert current["history"] == original["history"] == []
    assert current["waiting_for_question"] is True
    assert all(call.args[0] != "Old answer" for call in incoming.message.reply_text.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["replacement", "document", "retirement", "privacy"])
async def test_role_completion_is_fenced_by_every_invalidation(monkeypatch, transition):
    incoming, progress = update()
    state.begin_custom_role_creation(912)
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        "app.process_policies.execute_text_process",
        held_provider(started, release, '{"title":"Old","purpose":"Purpose","prompt":"Prompt"}'),
    )
    task = asyncio.create_task(
        msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "Old")
    )
    await asyncio.wait_for(started.wait(), 2)
    if transition == "replacement":
        state.begin_custom_role_creation(912)
    elif transition == "document":
        state.set_document_mode(912, True, 3)
    elif transition == "retirement":
        state.purge_user_runtime_state(912)
    else:
        monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", AsyncMock(return_value=False))
    release.set()
    await task
    assert state.get_generated_role(912) is None
    progress.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_waits_for_provider_cleanup_before_acknowledging(monkeypatch):
    incoming, _ = update()
    state.begin_custom_role_creation(912)
    owner = state.get_user_state(912).interaction
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        "app.process_policies.execute_text_process",
        held_provider(started, release, '{"title":"Old","purpose":"Purpose","prompt":"Prompt"}'),
    )
    monkeypatch.setattr(cb_roles.menus, "get_roles_menu_content", AsyncMock(return_value=("Roles", None, None)))
    task = asyncio.create_task(
        msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "Old")
    )
    await asyncio.wait_for(started.wait(), 2)
    cancellation, _ = update()
    query = callback(cancellation, f"role_create_cancel:{owner.token}")
    cancel_task = asyncio.create_task(cb_roles.role_create_cancel_callback(cancellation, SimpleNamespace(user_data={})))
    await asyncio.sleep(0)
    assert not cancel_task.done()
    query.answer.assert_not_awaited()
    release.set()
    await asyncio.gather(task, cancel_task)
    query.answer.assert_awaited_once_with("❌ Создание роли отменено")
    assert not state.get_user_state(912)._interaction_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["role_custom_apply", "role_custom_save", "role_custom_retry", "role_create_cancel"])
async def test_old_preview_button_cannot_use_or_cancel_new_preview(monkeypatch, prefix):
    state.begin_custom_role_creation(912)
    owner = state.get_user_state(912).interaction
    state.set_generated_role(912, {"title": "New", "prompt": "New prompt", "_interaction_token": owner.token})
    state.set_last_custom_role_prompt(912, "New description")
    incoming, _ = update()
    callback(incoming, f"{prefix}:old-token")
    writer, provider = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cb_roles, "create_custom_role", writer)
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    handler = {
        "role_custom_apply": cb_roles.role_custom_apply_callback,
        "role_custom_save": cb_roles.role_custom_save_callback,
        "role_custom_retry": cb_roles.role_custom_retry_callback,
        "role_create_cancel": cb_roles.role_create_cancel_callback,
    }[prefix]
    await handler(incoming, SimpleNamespace(user_data={}))
    writer.assert_not_awaited()
    provider.assert_not_awaited()
    assert state.get_generated_role(912)["title"] == "New"
    assert owner.current()


@pytest.mark.asyncio
async def test_first_role_request_creates_only_an_explicitly_missing_chat_generation(monkeypatch):
    state.begin_custom_role_creation(912)
    incoming, _ = update()
    monkeypatch.setattr(
        msg_roles,
        "get_user_chat",
        AsyncMock(return_value=SimpleNamespace(model="test-model", memory_epoch=0, _has_persisted_chat=False)),
    )
    ensure = AsyncMock(return_value=91)
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", ensure)
    provider = AsyncMock(return_value=('{"title":"Role","purpose":"Purpose","prompt":"Prompt"}', 1))
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    await msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "New")
    ensure.assert_awaited_once_with(912, expected_epoch=None)
    assert state.get_generated_role(912)["_memory_epoch"] == 91
    provider.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_private_lease_cannot_call_role_provider(monkeypatch):
    @asynccontextmanager
    async def denied(*args, **kwargs):
        yield False

    state.begin_custom_role_creation(912)
    incoming, _ = update()
    provider = AsyncMock()
    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", denied)
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    await msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "New")
    provider.assert_not_awaited()
    assert state.get_generated_role(912) is None
    assert "устарел" in incoming.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_missing_durable_generation_cannot_call_role_provider(monkeypatch):
    state.begin_custom_role_creation(912)
    incoming, _ = update()
    provider = AsyncMock(return_value=('{"title":"Role","purpose":"Purpose","prompt":"Prompt"}', 1))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=None))
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    await msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "New")
    provider.assert_not_awaited()
    assert state.get_generated_role(912) is None
    assert "устарел" in incoming.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_cancelled_ai_edit_cannot_replace_new_edit_input(monkeypatch):
    incoming, progress = update("Change tone")
    owner = state.begin_interaction(912, "role_edit")
    context = SimpleNamespace(
        user_data={
            "role_edit_token": owner.token,
            "edit_prompt_ai_role_id": 1,
            "edit_prompt_ai_role_key": "user_role:1",
            "edit_prompt_ai_current": "Old prompt",
        }
    )
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        "app.process_policies.execute_text_process", held_provider(started, release, "Old enhanced prompt")
    )
    task = asyncio.create_task(msg_roles.handle_edit_prompt(incoming, context, 912))
    await asyncio.wait_for(started.wait(), 2)
    new_owner = cb_roles._start_role_edit(912, context.user_data)
    context.user_data.update(edit_prompt_ai_role_id=2, edit_prompt_ai_current="New source")
    release.set()
    await task
    assert context.user_data["role_edit_token"] == new_owner.token
    assert context.user_data["edit_prompt_ai_role_id"] == 2
    assert "edit_prompt_ai_preview" not in context.user_data
    progress.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_ai_save_and_manual_save_cannot_use_new_payload(monkeypatch):
    incoming, _ = update()
    owner = state.begin_interaction(912, "role_edit")
    context = SimpleNamespace(
        user_data={
            "role_edit_token": owner.token,
            "edit_prompt_ai_save_role_id": 2,
            "edit_prompt_ai_save_role_key": "user_role:2",
            "edit_prompt_ai_preview": "New prompt",
        }
    )
    writer = AsyncMock()
    monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", writer)
    callback(incoming, "role_edit_ai_save:old-token")
    await cb_roles.role_edit_ai_save_callback(incoming, context)
    writer.assert_not_awaited()
    state.begin_manual_role_creation(912)
    state.set_manual_role_title(912, "New title")
    state.set_manual_role_prompt(912, "New prompt")
    callback(incoming, "role_manual_save:old-token")
    monkeypatch.setattr(cb_roles, "create_custom_role", writer)
    await cb_roles.role_manual_save_callback(incoming, context)
    writer.assert_not_awaited()
    assert state.get_manual_role_title(912) == "New title"


@pytest.mark.asyncio
async def test_request_cancellation_drains_an_sdk_that_suppresses_cancellation(monkeypatch):
    incoming, progress = update()
    state.begin_custom_role_creation(912)
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        "app.process_policies.execute_text_process",
        held_provider(started, release, '{"title":"Old","purpose":"Purpose","prompt":"Prompt"}'),
    )
    task = asyncio.create_task(
        msg_roles.handle_custom_role_generation(incoming, SimpleNamespace(user_data={}), 912, 912, "Old")
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not state.get_user_state(912)._interaction_tasks
    assert not state.get_user_state(912).generating_custom_role
    assert state.get_generated_role(912) is None
    progress.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_parent_cancellation_cannot_interrupt_child_cleanup():
    owner = state.begin_interaction(912, "role")
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    request = asyncio.create_task(state.run_interaction(owner, work))
    await asyncio.wait_for(started.wait(), 2)
    request.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    request.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
    premature = request.done()
    release.set()
    await asyncio.gather(request, return_exceptions=True)
    assert not premature
    assert cleaned.is_set()
    assert not state.get_user_state(912)._interaction_tasks


@pytest.mark.asyncio
async def test_cancelled_replacement_cannot_interrupt_predecessor_cleanup():
    owner = state.begin_interaction(912, "role")
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def work():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    first = asyncio.create_task(state.run_interaction(owner, work))
    await asyncio.wait_for(started.wait(), 2)
    replacement = state.begin_interaction(912, "role_edit")
    await asyncio.wait_for(cleaning.wait(), 2)
    replacement_work = AsyncMock()
    second = asyncio.create_task(state.run_interaction(replacement, replacement_work))
    for _ in range(3):
        await asyncio.sleep(0)
    second.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
    premature = second.done()
    release.set()
    await asyncio.gather(first, second, return_exceptions=True)
    assert not premature
    assert cleaned.is_set()
    replacement_work.assert_not_awaited()
    assert not state.get_user_state(912)._interaction_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["apply", "save", "manual", "edit"])
async def test_rejected_chat_write_never_reports_role_applied(monkeypatch, flow):
    incoming, _ = update()
    context = SimpleNamespace(user_data={})
    chat_state = SimpleNamespace(model="test-model", system_prompt="Old prompt", memory_epoch=7)
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=chat_state))
    writer = AsyncMock(return_value=False)
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    monkeypatch.setattr(cb_roles, "create_custom_role", AsyncMock())
    monkeypatch.setattr(cb_roles.role_conv_metrics, "record_custom_role_creation", AsyncMock())
    monkeypatch.setattr(cb_roles.menus, "get_roles_menu_content", AsyncMock(return_value=("Details", None, None)))
    if flow in {"apply", "save"}:
        state.begin_custom_role_creation(912)
        owner = state.get_user_state(912).interaction
        state.set_generated_role(912, {"title": "Role", "prompt": "New prompt", "_interaction_token": owner.token})
        prefix = "role_custom_save" if flow == "save" else "role_custom_apply"
        query = callback(incoming, f"{prefix}:{owner.token}")
        handler = cb_roles.role_custom_save_callback if flow == "save" else cb_roles.role_custom_apply_callback
    elif flow == "manual":
        state.begin_manual_role_creation(912)
        state.set_manual_role_title(912, "Manual role")
        state.set_manual_role_prompt(912, "New prompt")
        owner = state.get_user_state(912).interaction
        query = callback(incoming, f"role_manual_save:{owner.token}")
        handler = cb_roles.role_manual_save_callback
    else:
        owner = state.begin_interaction(912, "role_edit")
        context.user_data.update(
            role_edit_token=owner.token,
            edit_prompt_ai_save_role_id=2,
            edit_prompt_ai_save_role_key="user_role:2",
            edit_prompt_ai_preview="New prompt",
            edit_prompt_ai_current="Old prompt",
        )
        query = callback(incoming, f"role_edit_ai_save:{owner.token}")
        monkeypatch.setattr("app.repos.roles.get_custom_role_prompt", AsyncMock(return_value="Old prompt"))
        monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", AsyncMock(return_value=True))
        handler = cb_roles.role_edit_ai_save_callback

    await handler(incoming, context)

    writer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    text = query.edit_message_text.await_args.args[0]
    assert not text.startswith("✅")
    assert "сохранена и применена" not in text
    assert "не удалось" in text or "устарел" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["apply", "save", "manual", "edit"])
async def test_first_use_owned_role_write_keeps_exact_initialized_epoch(monkeypatch, flow):
    incoming, _ = update()
    context = SimpleNamespace(user_data={})
    chat = SimpleNamespace(model="test-model", system_prompt="Old", memory_epoch=0, _has_persisted_chat=False)
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=91))
    writer = AsyncMock(return_value=False)
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    monkeypatch.setattr(cb_roles, "create_custom_role", AsyncMock())
    monkeypatch.setattr(cb_roles.role_conv_metrics, "record_custom_role_creation", AsyncMock())
    if flow in {"apply", "save"}:
        state.begin_custom_role_creation(912)
        owner = state.get_user_state(912).interaction
        state.set_generated_role(912, {"title": "Role", "prompt": "New", "_interaction_token": owner.token})
        callback(incoming, f"role_custom_{flow}:{owner.token}")
        handler = cb_roles.role_custom_save_callback if flow == "save" else cb_roles.role_custom_apply_callback
    elif flow == "manual":
        state.begin_manual_role_creation(912)
        state.set_manual_role_title(912, "Role")
        state.set_manual_role_prompt(912, "New")
        owner = state.get_user_state(912).interaction
        callback(incoming, f"role_manual_save:{owner.token}")
        handler = cb_roles.role_manual_save_callback
    else:
        owner = state.begin_interaction(912, "role_edit")
        context.user_data.update(
            role_edit_token=owner.token,
            edit_prompt_ai_save_role_id=2,
            edit_prompt_ai_save_role_key="user_role:2",
            edit_prompt_ai_preview="New",
            edit_prompt_ai_current="Old",
        )
        callback(incoming, f"role_edit_ai_save:{owner.token}")
        monkeypatch.setattr("app.repos.roles.get_custom_role_prompt", AsyncMock(return_value="Old"))
        monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", AsyncMock(return_value=True))
        handler = cb_roles.role_edit_ai_save_callback
    await handler(incoming, context)
    writer.assert_awaited_once_with(912, chat, expected_epoch=91)
    assert chat.memory_epoch == 91
    assert chat._has_persisted_chat is True
    assert "устарел" in incoming.callback_query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_first_end_hydrates_other_persisted_fields_before_clearing_tarot(monkeypatch):
    incoming, _ = update()
    saved = {
        "document_mode": True,
        "selected_document_id": 12,
        "role_diaries": {"saved-role": ["Synthetic saved entry"]},
        "tarot_mode": True,
        "tarot_session": {"history": []},
    }
    load = AsyncMock(return_value=saved)
    monkeypatch.setattr("app.repos.users.load_user_state", load)
    snapshots = []
    monkeypatch.setattr(
        state,
        "_schedule_persist",
        lambda current: snapshots.append(
            (current._loaded_from_db, current.document_mode, current.selected_document_id, current.role_diaries)
        ),
    )
    await tarot_chat._finish_tarot_session(incoming, 912)
    load.assert_awaited_once_with(912)
    assert snapshots and snapshots[-1] == (True, True, 12, saved["role_diaries"])
    assert not state.is_in_tarot_mode(912)
    assert state.get_tarot_session(912) is None


@pytest.mark.asyncio
async def test_first_end_hydration_failure_does_not_mutate_or_persist(monkeypatch):
    incoming, _ = update()
    monkeypatch.setattr("app.repos.users.load_user_state", AsyncMock(side_effect=RuntimeError("synthetic outage")))
    persist = MagicMock()
    monkeypatch.setattr(state, "_schedule_persist", persist)
    with pytest.raises(RuntimeError, match="synthetic outage"):
        await tarot_chat._finish_tarot_session(incoming, 912)
    persist.assert_not_called()
    incoming.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "persisted-token"])
async def test_first_idle_callback_hydrates_legacy_and_tokenized_choices(monkeypatch, token):
    incoming, _ = update()
    saved = {"pending_idle_text": "Synthetic saved question", "last_activity_at": time.time()}
    if token is not None:
        saved["pending_idle_token"] = token
    monkeypatch.setattr(
        "app.repos.users.load_user_state",
        AsyncMock(
            return_value={
                "tarot_mode": True,
                "tarot_session": saved,
                "role_diaries": {"saved-role": ["Saved entry"]},
            }
        ),
    )
    callback(incoming, "tarot_idle:continue" + (f":{token}" if token is not None else ""))
    turn = AsyncMock()
    monkeypatch.setattr(tarot_chat, "handle_tarot_message", turn)
    await tarot_chat.handle_tarot_idle_choice_callback(incoming, SimpleNamespace(user_data={}))
    turn.assert_awaited_once()
    assert turn.await_args.args[0].message.text == "Synthetic saved question"
    assert state.get_user_state(912).role_diaries == {"saved-role": ["Saved entry"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["role", "manual", "edit_ai", "edit_manual", "tarot"])
@pytest.mark.parametrize("busy", ["flag", "lock"])
async def test_ordinary_busy_rejects_new_role_and_tarot_input(monkeypatch, flow, busy):
    from app.handlers.cmd_tarot import tarot_command

    incoming, _ = update()
    current = state.get_user_state(912)
    current._loaded_from_db = True
    old_owner = state.begin_interaction(912, "tarot")
    data = {
        "role": "role_create",
        "manual": "role_create_manual",
        "edit_ai": "role_edit_ai:user_role:2",
        "edit_manual": "role_edit_manual:user_role:2",
        "tarot": "synthetic",
    }[flow]
    query = callback(incoming, data)
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr(cb_roles, "get_role_data", AsyncMock(return_value={"prompt": "Old"}))
    if busy == "lock":
        await current.lock.acquire()
    else:
        current.is_processing = True
    try:
        handler = {
            "role": cb_roles.role_create_callback,
            "manual": cb_roles.role_create_manual_callback,
            "edit_ai": cb_roles.role_edit_ai_callback,
            "edit_manual": cb_roles.role_edit_manual_callback,
            "tarot": tarot_command,
        }[flow]
        await handler(incoming, context)
        assert current.interaction is old_owner
        assert not current.awaiting_custom_role_input
        assert not current.awaiting_manual_role_title
        assert not context.user_data
        query.edit_message_text.assert_not_awaited()
        context.bot.send_message.assert_not_awaited()
    finally:
        if busy == "lock":
            current.lock.release()
        current.is_processing = False


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["apply", "save", "manual", "edit"])
async def test_ordinary_busy_rejects_owned_role_writes(monkeypatch, flow):
    incoming, _ = update()
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(
        cb_roles,
        "get_user_chat",
        AsyncMock(
            return_value=SimpleNamespace(
                model="test-model",
                memory_epoch=7,
                system_prompt="Old",
            )
        ),
    )
    state.get_user_state(912)._loaded_from_db = True
    if flow in {"apply", "save"}:
        state.begin_custom_role_creation(912)
        owner = state.get_user_state(912).interaction
        state.set_generated_role(912, {"title": "Role", "prompt": "New", "_interaction_token": owner.token})
        callback(incoming, f"role_custom_{flow}:{owner.token}")
        handler = cb_roles.role_custom_save_callback if flow == "save" else cb_roles.role_custom_apply_callback
    elif flow == "manual":
        state.begin_manual_role_creation(912)
        state.set_manual_role_title(912, "Role")
        state.set_manual_role_prompt(912, "New")
        owner = state.get_user_state(912).interaction
        callback(incoming, f"role_manual_save:{owner.token}")
        handler = cb_roles.role_manual_save_callback
    else:
        owner = state.begin_interaction(912, "role_edit")
        context.user_data.update(
            role_edit_token=owner.token,
            edit_prompt_ai_save_role_id=2,
            edit_prompt_ai_preview="New",
            edit_prompt_ai_save_role_key="user_role:2",
            edit_prompt_ai_current="Old",
        )
        callback(incoming, f"role_edit_ai_save:{owner.token}")
        monkeypatch.setattr("app.repos.roles.get_custom_role_prompt", AsyncMock(return_value="Old"))
        handler = cb_roles.role_edit_ai_save_callback
    writer = AsyncMock(return_value=True)
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    monkeypatch.setattr(cb_roles, "create_custom_role", writer)
    monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", writer)
    state.get_user_state(912).is_processing = True
    try:
        await handler(incoming, context)
        writer.assert_not_awaited()
        assert owner.current()
    finally:
        state.get_user_state(912).is_processing = False


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["role", "manual", "edit_ai", "edit_manual"])
async def test_role_input_leaves_tarot_before_publishing_input(monkeypatch, flow):
    from telegram import ReplyKeyboardRemove

    incoming, _ = update()
    current = state.get_user_state(912)
    current._loaded_from_db = True
    tarot_session()
    query = callback(
        incoming,
        {
            "role": "role_create",
            "manual": "role_create_manual",
            "edit_ai": "role_edit_ai:user_role:2",
            "edit_manual": "role_edit_manual:user_role:2",
        }[flow],
    )
    monkeypatch.setattr(cb_roles, "get_role_data", AsyncMock(return_value={"prompt": "Old"}))
    handler = {
        "role": cb_roles.role_create_callback,
        "manual": cb_roles.role_create_manual_callback,
        "edit_ai": cb_roles.role_edit_ai_callback,
        "edit_manual": cb_roles.role_edit_manual_callback,
    }[flow]
    await handler(incoming, SimpleNamespace(user_data={}))
    assert not state.is_in_tarot_mode(912)
    assert state.get_tarot_session(912) is None
    assert current.interaction.kind == {"role": "role", "manual": "manual_role"}.get(flow, "role_edit")
    assert isinstance(incoming.message.reply_text.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["generate", "manual_input", "manual_edit", "ai_edit", "tarot_turn"])
async def test_ordinary_busy_rejects_direct_interaction_text(monkeypatch, flow):
    incoming, _ = update("Synthetic input")
    context = SimpleNamespace(user_data={})
    current = state.get_user_state(912)
    current._loaded_from_db = True
    if flow == "generate":
        state.begin_custom_role_creation(912)
    elif flow == "manual_input":
        state.begin_manual_role_creation(912)
    elif flow in {"manual_edit", "ai_edit"}:
        owner = state.begin_interaction(912, "role_edit")
        context.user_data["role_edit_token"] = owner.token
        context.user_data["edit_prompt_ai_role_id" if flow == "ai_edit" else "edit_prompt_role_id"] = 2
    else:
        tarot_session()
    owner = current.interaction
    original_session = current.tarot_session
    provider = AsyncMock(return_value=("Synthetic answer", 1))
    writer = AsyncMock(return_value=True)
    monkeypatch.setattr("app.process_policies.execute_text_process", provider)
    monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", writer)
    current.is_processing = True
    try:
        if flow == "generate":
            await msg_roles.handle_custom_role_generation(incoming, context, 912, 912, incoming.message.text)
        elif flow == "manual_input":
            await msg_roles.handle_manual_role_input(incoming, context, 912)
        elif flow in {"manual_edit", "ai_edit"}:
            await msg_roles.handle_edit_prompt(incoming, context, 912)
        else:
            await tarot_chat.handle_tarot_message(incoming, context)
        provider.assert_not_awaited()
        writer.assert_not_awaited()
        assert current.interaction is owner
        assert current.tarot_session is original_session
        assert not current.manual_role_title
        assert "Дождитесь" in incoming.message.reply_text.await_args.args[0]
    finally:
        current.is_processing = False


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["role_cancel", "tarot_end"])
async def test_ordinary_busy_does_not_block_explicit_cancel_or_end(monkeypatch, flow):
    incoming, _ = update()
    context = SimpleNamespace(user_data={})
    current = state.get_user_state(912)
    current._loaded_from_db = True
    if flow == "role_cancel":
        state.begin_custom_role_creation(912)
        callback(incoming, f"role_create_cancel:{current.interaction.token}")
        monkeypatch.setattr(cb_roles.menus, "get_roles_menu_content", AsyncMock(return_value=("Menu", None, None)))
    else:
        tarot_session()
    current.is_processing = True
    try:
        if flow == "role_cancel":
            await cb_roles.role_create_cancel_callback(incoming, context)
        else:
            await tarot_chat.handle_tarot_end_session_message(incoming, context)
        assert current.interaction is None
        assert not current.awaiting_custom_role_input
        assert not current.tarot_mode
        assert current.is_processing is True
    finally:
        current.is_processing = False


@pytest.mark.asyncio
async def test_busy_idle_choice_keeps_pending_question(monkeypatch):
    incoming, _ = update()
    current = state.get_user_state(912)
    current._loaded_from_db = True
    pending = {"pending_idle_text": "Synthetic pending question", "pending_idle_token": "token"}
    state.set_tarot_mode(912, True)
    state.set_tarot_session(912, pending)
    callback(incoming, "tarot_idle:continue:token")
    turn = AsyncMock()
    monkeypatch.setattr(tarot_chat, "handle_tarot_message", turn)
    current.is_processing = True
    try:
        await tarot_chat.handle_tarot_idle_choice_callback(incoming, SimpleNamespace(user_data={}))
        assert state.get_tarot_session(912) is pending
        assert pending["pending_idle_text"] == "Synthetic pending question"
        turn.assert_not_awaited()
    finally:
        current.is_processing = False


@pytest.mark.asyncio
async def test_first_use_direct_role_edit_fences_its_chat_write(monkeypatch):
    incoming, _ = update("New prompt")
    chat = SimpleNamespace(model="test-model", system_prompt="Old", memory_epoch=0, _has_persisted_chat=False)
    owner = state.begin_interaction(912, "role_edit")
    context = SimpleNamespace(user_data={"role_edit_token": owner.token, "edit_prompt_role_id": 2})
    monkeypatch.setattr(msg_roles, "get_user_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=91))
    monkeypatch.setattr("app.repos.roles.get_custom_role_prompt", AsyncMock(return_value="Old"))
    monkeypatch.setattr("app.repos.roles.update_custom_role_prompt", AsyncMock(return_value=True))
    writer = AsyncMock(return_value=False)
    monkeypatch.setattr("app.repos.chats.update_user_chat", writer)
    assert await msg_roles.handle_edit_prompt(incoming, context, 912)
    writer.assert_awaited_once_with(912, chat, expected_epoch=91)
    assert "не удалось" in incoming.message.reply_text.await_args.args[0].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["regular_apply", "regular_clear", "retry", "tweak"])
async def test_busy_role_callback_keeps_other_request_ownership(monkeypatch, flow):
    incoming, _ = update()
    current = state.get_user_state(912)
    current._loaded_from_db = True
    owner = state.begin_interaction(912, "role")
    query = callback(
        incoming,
        {
            "regular_apply": "role_apply:user_role:2",
            "regular_clear": "role_clear",
            "retry": "role_custom_retry",
            "tweak": "role_edit_ai_tweak:user_role:2",
        }[flow],
    )
    writer = AsyncMock()
    monkeypatch.setattr(cb_roles, "get_user_chat", writer)
    monkeypatch.setattr(cb_roles, "get_role_data", writer)
    monkeypatch.setattr(cb_roles, "update_user_chat", writer)
    current.is_processing = True
    try:
        handler = {
            "regular_apply": cb_roles.role_apply_callback,
            "regular_clear": cb_roles.role_clear_callback,
            "retry": cb_roles.role_custom_retry_callback,
            "tweak": cb_roles.role_edit_ai_tweak_callback,
        }[flow]
        await handler(incoming, SimpleNamespace(user_data={}))
        writer.assert_not_awaited()
        assert current.interaction is owner
        query.edit_message_text.assert_not_awaited()
        assert "Дождитесь" in query.answer.await_args.args[0]
    finally:
        current.is_processing = False

"""Destructive callbacks scope mutations and publish success only after success."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_conversations as conversations
from app.handlers import cb_documents as documents


def callback(data, user_id=101):
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock(), edit_message_text=AsyncMock()
    )
    return SimpleNamespace(callback_query=query), SimpleNamespace(user_data={})


@pytest.fixture
def document_store(monkeypatch):
    rows = {(101, 11): {"id": 11, "filename": "synthetic.txt"}, (202, 22): {"id": 22, "filename": "other.txt"}}
    mutations = []
    states = {uid: state.UserState(uid) for uid in (101, 202)}
    monkeypatch.setattr(state, "get_user_state", states.__getitem__)
    monkeypatch.setattr(state, "_schedule_persist", lambda chat: None)
    state.set_document_mode(101, True, 11)
    state.set_document_mode(202, True, 22)
    states[101].last_document_message_id = 77

    async def lookup(doc_id, user_id):
        return rows.get((user_id, doc_id))

    async def delete(doc_id, user_id):
        mutations.append((doc_id, user_id))
        return rows.pop((user_id, doc_id), None) is not None

    async def clear(user_id):
        ids = [doc_id for owner, doc_id in rows if owner == user_id]
        for doc_id in ids:
            rows.pop((user_id, doc_id))
        mutations.append(("all", user_id))
        return len(ids)

    monkeypatch.setattr(documents, "get_document_by_id", lookup)
    monkeypatch.setattr(documents, "delete_user_document", delete)
    monkeypatch.setattr(documents, "delete_all_user_documents", clear)
    monkeypatch.setattr(documents, "get_user_documents", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        documents.menus, "get_documents_menu_content", AsyncMock(return_value=("Document list", None, None))
    )
    return SimpleNamespace(rows=rows, mutations=mutations, states=states)


def _selected(store):
    return {uid: chat.selected_document_id for uid, chat in store.states.items() if chat.document_mode}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "doc",
        "doc:delete_document",
        "doc:delete_document:nope",
        "doc:delete_document:0",
        "doc:delete_document:-1",
        "doc:delete_document:11:extra",
    ],
)
async def test_document_malformed_delete_does_not_mutate(document_store, payload):
    update, context = callback(payload)
    await documents.document_callback(update, context)
    assert document_store.mutations == []
    assert _selected(document_store) == {101: 11, 202: 22}
    assert update.callback_query.answer.await_count >= 1


@pytest.mark.asyncio
async def test_document_foreign_id_cannot_delete_or_clear_selection(document_store):
    update, context = callback("doc:delete_document:22")
    await documents.document_callback(update, context)
    assert document_store.mutations == []
    assert (202, 22) in document_store.rows
    assert _selected(document_store) == {101: 11, 202: 22}
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [11, 99])
async def test_document_delete_cleans_only_matching_selection_and_replay_is_stale(document_store, selected):
    document_store.states[101].selected_document_id = selected
    update, context = callback("doc:delete_document:11")
    await documents.document_callback(update, context)
    assert document_store.mutations == [(11, 101)]
    assert _selected(document_store) == ({202: 22} if selected == 11 else {101: 99, 202: 22})
    assert document_store.states[101].last_document_message_id == (None if selected == 11 else 77)
    assert (202, 22) in document_store.rows
    assert "удален" in update.callback_query.answer.await_args.args[0]
    await documents.document_callback(update, context)
    assert document_store.mutations == [(11, 101)]
    assert "удален." not in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("repository unavailable")])
async def test_document_delete_failure_never_claims_success(monkeypatch, document_store, failure):
    delete = AsyncMock(return_value=False, side_effect=failure if isinstance(failure, Exception) else None)
    monkeypatch.setattr(documents, "delete_user_document", delete)
    update, context = callback("doc:delete_document:11")
    await documents.document_callback(update, context)
    delete.assert_awaited_once_with(11, 101)
    assert _selected(document_store) == {101: 11, 202: 22}
    update.callback_query.edit_message_text.assert_not_awaited()
    assert "удален." not in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_clear_all_is_scoped_and_replay_does_not_claim_a_second_delete(document_store):
    update, context = callback("doc:clear_all_confirm")
    await documents.document_callback(update, context)
    assert document_store.rows == {(202, 22): {"id": 22, "filename": "other.txt"}}
    assert _selected(document_store) == {202: 22}
    assert "Удалено 1" in update.callback_query.answer.await_args.args[0]
    await documents.document_callback(update, context)
    assert "Удалено" not in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_clear_all_repository_exception_preserves_selection(monkeypatch, document_store):
    delete = AsyncMock(side_effect=RuntimeError("repository unavailable"))
    monkeypatch.setattr(documents, "delete_all_user_documents", delete)
    update, context = callback("doc:clear_all_confirm")
    await documents.document_callback(update, context)
    delete.assert_awaited_once_with(101)
    assert _selected(document_store) == {101: 11, 202: 22}
    update.callback_query.edit_message_text.assert_not_awaited()
    assert "Удалено" not in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_conversation_switch_success_calls_exact_owner_and_id(conversation_store):
    update, context = callback("conv_switch_to:11")
    await conversations.conv_switch_to_callback(update, context)
    assert conversation_store.switched == [(101, 11)]
    assert conversation_store.active == {101: 11}
    assert "✅" in update.callback_query.answer.await_args.args[0]


@pytest.fixture
def conversation_store(monkeypatch):
    rows = {(101, 11), (202, 22)}
    deleted = []
    switched = []
    active = {}

    async def delete(uid, cid):
        deleted.append((uid, cid))
        if (uid, cid) not in rows:
            return False
        rows.remove((uid, cid))
        return True

    async def switch(uid, cid):
        switched.append((uid, cid))
        if (uid, cid) not in rows:
            return False
        active[uid] = cid
        return True

    monkeypatch.setattr(conversations, "delete_conversation", delete)
    monkeypatch.setattr(conversations, "switch_to_conversation", switch)
    monkeypatch.setattr(conversations.role_conv_metrics, "record_conversation_deleted", AsyncMock())
    monkeypatch.setattr(conversations.role_conv_metrics, "record_conversation_switched", AsyncMock())
    monkeypatch.setattr(
        conversations.menus, "get_conversations_menu_content", AsyncMock(return_value=("Conversation list", None, None))
    )
    return SimpleNamespace(rows=rows, deleted=deleted, switched=switched, active=active)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete_confirm", "switch_to"])
@pytest.mark.parametrize("suffix", ["", ":nope", ":0", ":-1", ":11:extra"])
async def test_conversation_malformed_callback_does_not_mutate(conversation_store, action, suffix):
    update, context = callback(f"conv_{action}{suffix}")
    await getattr(conversations, f"conv_{action}_callback")(update, context)
    assert conversation_store.deleted == []
    assert conversation_store.switched == []
    assert update.callback_query.answer.await_count >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete_confirm", "switch_to"])
async def test_conversation_foreign_id_is_scoped_and_never_claims_success(conversation_store, action):
    update, context = callback(f"conv_{action}:22")
    await getattr(conversations, f"conv_{action}_callback")(update, context)
    calls = conversation_store.deleted if action == "delete_confirm" else conversation_store.switched
    assert calls == [(101, 22)]
    assert conversation_store.rows == {(101, 11), (202, 22)}
    assert conversation_store.active == {}
    assert "✅" not in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_conversation_delete_success_then_stale_replay(conversation_store):
    update, context = callback("conv_delete_confirm:11")
    await conversations.conv_delete_confirm_callback(update, context)
    assert conversation_store.rows == {(202, 22)}
    assert "✅" in update.callback_query.answer.await_args.args[0]
    await conversations.conv_delete_confirm_callback(update, context)
    assert conversation_store.deleted == [(101, 11), (101, 11)]
    assert "✅" not in update.callback_query.answer.await_args.args[0]
    conversations.role_conv_metrics.record_conversation_deleted.assert_awaited_once()


@pytest.mark.asyncio
async def test_conversation_delete_repository_exception_is_controlled(monkeypatch, conversation_store):
    delete = AsyncMock(side_effect=RuntimeError("repository unavailable"))
    monkeypatch.setattr(conversations, "delete_conversation", delete)
    update, context = callback("conv_delete_confirm:11")
    await conversations.conv_delete_confirm_callback(update, context)
    delete.assert_awaited_once_with(101, 11)
    assert conversation_store.rows == {(101, 11), (202, 22)}
    assert "✅" not in update.callback_query.answer.await_args.args[0]

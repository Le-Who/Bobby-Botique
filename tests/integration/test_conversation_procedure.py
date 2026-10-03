"""Stored conversation export preserves turns and enforces target ownership."""

import pytest

from app.repos import chats, conversations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


async def test_save_conversation_keeps_model_and_legacy_assistant_turns_in_order(db_conn_with_user, test_user_id):
    state = await chats.get_user_chat(test_user_id)
    assert state is not None
    state.history = [
        {"role": "user", "parts": ["First question"]},
        {"role": "model", "parts": ["First reply"]},
        {"role": "user", "parts": ["/start"]},
        {"role": "assistant", "parts": ["Legacy reply"]},
        {"role": "system", "parts": ["Internal context"]},
        {"role": "user", "parts": ["Second question"]},
        {"role": "model", "parts": ["Second reply"]},
    ]
    assert await chats.update_user_chat(test_user_id, state)

    conversation_id = await conversations.save_conversation(test_user_id, "Exported chat")
    assert conversation_id is not None
    saved = await conversations.get_conversation_messages(conversation_id, test_user_id)
    assert saved is not None
    assert [(row["role"], row["content"]) for row in saved] == [
        ("user", "First question"),
        ("model", "First reply"),
        ("assistant", "Legacy reply"),
        ("user", "Second question"),
        ("model", "Second reply"),
    ]


async def test_export_procedure_cannot_append_to_another_users_conversation(db_conn_with_user, test_user_id):
    conversation_id = await conversations.save_conversation(test_user_id, "Owner's empty chat")
    assert conversation_id is not None
    other_user_id = 888889
    await db_conn_with_user.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
    other_state = await chats.get_user_chat(other_user_id)
    assert other_state is not None
    other_state.history = [{"role": "user", "parts": ["Other user's private turn"]}]
    assert await chats.update_user_chat(other_user_id, other_state)

    await db_conn_with_user.execute(
        "CALL save_chat_to_conversation($1::bigint, $2::integer)", other_user_id, conversation_id
    )

    assert await conversations.get_conversation_messages(conversation_id, test_user_id) == []
    assert await conversations.get_conversation_messages(conversation_id, other_user_id) is None

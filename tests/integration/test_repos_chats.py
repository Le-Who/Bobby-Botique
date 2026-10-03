"""Integration tests for the real chat-state repository APIs."""

from types import SimpleNamespace

import pytest

from app.repos import chats

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


@pytest.fixture(autouse=True)
def default_chat_model(monkeypatch):
    monkeypatch.setattr(chats, "settings", SimpleNamespace(DEFAULT_MODEL="gemini-3.1-flash-lite"))


class TestChatStateLifecycle:
    async def test_get_user_chat_returns_defaults_without_creating_row(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)

        assert state is not None
        assert state.model == "gemini-3.1-flash-lite"
        assert state.history == []
        assert state.token_count == 0
        assert state.search_enabled is False
        assert state._has_persisted_chat is False
        assert await db_conn_with_user.fetchval("SELECT COUNT(*) FROM chats WHERE user_id = $1", test_user_id) == 0

    async def test_ensure_chat_generation_creates_missing_row(self, db_conn_with_user, test_user_id):
        epoch = await chats.ensure_chat_generation(test_user_id, expected_epoch=None)

        assert epoch is not None
        assert (
            await db_conn_with_user.fetchval("SELECT memory_epoch FROM chats WHERE user_id = $1", test_user_id) == epoch
        )
        assert await chats.ensure_chat_generation(test_user_id, expected_epoch=epoch) == epoch
        assert await chats.ensure_chat_generation(test_user_id, expected_epoch=epoch + 1) is None

    async def test_update_chat_model_and_history(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.model = "gemini-3.1-pro-preview"
        state.token_count = 150
        state.history = [{"role": "user", "parts": ["Hello"]}, {"role": "model", "content": "Hi!"}]

        assert await chats.update_user_chat(test_user_id, state) is True

        row = await db_conn_with_user.fetchrow("SELECT model, token_count FROM chats WHERE user_id = $1", test_user_id)
        assert dict(row) == {"model": "gemini-3.1-pro-preview", "token_count": 150}
        loaded = await chats.get_user_chat(test_user_id)
        assert loaded is not None
        assert loaded.history == [{"role": "user", "parts": ["Hello"]}, {"role": "model", "parts": ["Hi!"]}]
        assert loaded._has_persisted_chat is True

    async def test_clear_chat_resets_state(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.history = [{"role": "user", "parts": ["old"]}]
        state.token_count = 500
        state.context_summary = "Previous summary"
        assert await chats.update_user_chat(test_user_id, state)

        loaded = await chats.get_user_chat(test_user_id)
        assert loaded is not None
        loaded.history = []
        loaded.token_count = 0
        loaded.context_summary = None
        assert await chats.update_user_chat(test_user_id, loaded)

        row = await db_conn_with_user.fetchrow(
            "SELECT token_count, context_summary FROM chats WHERE user_id = $1", test_user_id
        )
        assert dict(row) == {"token_count": 0, "context_summary": None}
        assert (
            await db_conn_with_user.fetchval(
                "SELECT COUNT(*) FROM active_chat_messages WHERE user_id = $1", test_user_id
            )
            == 0
        )

    async def test_update_thinking_level(self, db_conn_with_user, test_user_id):
        assert await chats.ensure_chat_generation(test_user_id, expected_epoch=None) is not None

        await chats.update_thinking_level(test_user_id, "high")
        assert (
            await db_conn_with_user.fetchval("SELECT thinking_level FROM chats WHERE user_id = $1", test_user_id)
            == "high"
        )

        await chats.update_thinking_level(test_user_id, None)
        assert (
            await db_conn_with_user.fetchval("SELECT thinking_level FROM chats WHERE user_id = $1", test_user_id)
            is None
        )

    async def test_stale_epoch_cannot_overwrite_chat_or_messages(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.history = [{"role": "user", "parts": ["Current message"]}]
        state.token_count = 25
        assert await chats.update_user_chat(test_user_id, state)
        stale_epoch = state.memory_epoch + 1
        state.history = [{"role": "user", "parts": ["Stale replacement"]}]
        state.token_count = 999

        assert (
            await chats.update_user_chat(test_user_id, state, rewrite_history=True, expected_epoch=stale_epoch) is False
        )

        loaded = await chats.get_user_chat(test_user_id)
        assert loaded is not None
        assert loaded.token_count == 25
        assert loaded.history == [{"role": "user", "parts": ["Current message"]}]


class TestActiveChatMessages:
    async def test_append_history_does_not_duplicate_saved_messages(self, db_conn_with_user, test_user_id):
        state = await chats.get_user_chat(test_user_id)
        assert state is not None
        state.history = [
            {"role": "user", "parts": ["What is Python?"]},
            {"role": "model", "parts": ["Python is a programming language."]},
            {"role": "user", "parts": ["Tell me more"]},
        ]
        assert await chats.update_user_chat(test_user_id, state)
        loaded = await chats.get_user_chat(test_user_id)
        assert loaded is not None
        loaded.history.append({"role": "model", "parts": ["It supports async code."]})

        assert await chats.update_user_chat(test_user_id, loaded)
        assert await chats.update_user_chat(test_user_id, loaded)

        rows = await db_conn_with_user.fetch(
            "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id", test_user_id
        )
        assert [(row["role"], row["content"]) for row in rows] == [
            ("user", "What is Python?"),
            ("model", "Python is a programming language."),
            ("user", "Tell me more"),
            ("model", "It supports async code."),
        ]

    async def test_rewrite_and_clear_do_not_modify_other_user(self, db_conn_with_user, test_user_id):
        other_user_id = 888888
        await db_conn_with_user.execute("INSERT INTO users (user_id) VALUES ($1)", other_user_id)
        for user_id, content in ((test_user_id, "Own message"), (other_user_id, "Other user's message")):
            state = await chats.get_user_chat(user_id)
            assert state is not None
            state.history = [{"role": "user", "parts": [content]}]
            state.token_count = 50
            assert await chats.update_user_chat(user_id, state)

        own = await chats.get_user_chat(test_user_id)
        assert own is not None
        own.history = [{"role": "model", "parts": ["Replacement"]}]
        assert await chats.update_user_chat(test_user_id, own, rewrite_history=True)
        own.history = []
        assert await chats.update_user_chat(test_user_id, own)

        assert (
            await db_conn_with_user.fetchval(
                "SELECT COUNT(*) FROM active_chat_messages WHERE user_id = $1", test_user_id
            )
            == 0
        )
        other = await chats.get_user_chat(other_user_id)
        assert other is not None
        assert other.token_count == 50
        assert other.history == [{"role": "user", "parts": ["Other user's message"]}]

"""Tests for app.state — UserState lifecycle, public API, state transitions."""

from unittest.mock import patch

import pytest

from app.state import (
    UserState,
    begin_custom_role_creation,
    begin_manual_role_creation,
    clear_custom_role_state,
    clear_document_state,
    clear_manual_role_state,
    finish_manual_role_input,
    get_generated_role,
    get_last_custom_role_prompt,
    get_last_sent_message,
    get_manual_role_prompt,
    get_manual_role_title,
    get_selected_document_id,
    get_user_lock,
    get_user_state,
    is_awaiting_custom_role_input,
    is_awaiting_manual_role_prompt,
    is_awaiting_manual_role_title,
    is_in_document_mode,
    set_document_mode,
    set_generated_role,
    set_generating_custom_role,
    set_last_custom_role_prompt,
    set_last_sent_message,
    set_manual_role_prompt,
    set_manual_role_title,
)


@pytest.fixture(autouse=True)
def _suppress_persist():
    """Prevent DB persistence calls during unit tests."""
    with patch("app.state._schedule_persist"):
        yield


# Use unique user IDs per test to avoid cross-test state pollution
_UID = iter(range(900_000, 999_999))


def uid():
    return next(_UID)


# ═══════════════════════════════════════════════════════════════════════════════
# UserState init
# ═══════════════════════════════════════════════════════════════════════════════


class TestUserStateInit:
    def test_defaults_are_all_false_or_none(self):
        s = UserState(user_id=1)
        assert s.document_mode is False
        assert s.selected_document_id is None
        assert s.awaiting_custom_role_input is False
        assert s.generated_role is None
        assert s.generating_custom_role is False
        assert s._loaded_from_db is False

    def test_lock_is_asyncio_lock(self):
        import asyncio

        s = UserState(user_id=2)
        assert isinstance(s.lock, asyncio.Lock)


# ═══════════════════════════════════════════════════════════════════════════════
# Document mode lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestDocumentMode:
    def test_set_document_mode_on(self):
        u = uid()
        set_document_mode(u, True, document_id=42)
        assert is_in_document_mode(u) is True
        assert get_selected_document_id(u) == 42

    def test_set_document_mode_off_clears_doc_id(self):
        u = uid()
        set_document_mode(u, True, document_id=42)
        set_document_mode(u, False)
        assert is_in_document_mode(u) is False
        assert get_selected_document_id(u) is None

    def test_clear_document_state_resets_all(self):
        u = uid()
        set_document_mode(u, True, document_id=99)
        clear_document_state(u)
        assert is_in_document_mode(u) is False
        assert get_selected_document_id(u) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Custom role creation lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestCustomRoleLifecycle:
    def test_begin_sets_awaiting(self):
        u = uid()
        begin_custom_role_creation(u)
        assert is_awaiting_custom_role_input(u) is True
        assert get_generated_role(u) is None

    def test_set_generated_role_clears_awaiting(self):
        u = uid()
        begin_custom_role_creation(u)
        role = {"title": "Test", "prompt": "Be helpful"}
        set_generated_role(u, role)
        assert get_generated_role(u) == role
        assert is_awaiting_custom_role_input(u) is False

    def test_clear_custom_role_resets_all(self):
        u = uid()
        begin_custom_role_creation(u)
        set_last_custom_role_prompt(u, "test prompt")
        clear_custom_role_state(u)
        assert is_awaiting_custom_role_input(u) is False
        assert get_last_custom_role_prompt(u) is None

    def test_set_generating_flag(self):
        u = uid()
        set_generating_custom_role(u, True)
        assert get_user_state(u).generating_custom_role is True
        set_generating_custom_role(u, False)
        assert get_user_state(u).generating_custom_role is False


# ═══════════════════════════════════════════════════════════════════════════════
# Manual role creation lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestManualRoleLifecycle:
    def test_begin_manual_waits_for_title(self):
        u = uid()
        begin_manual_role_creation(u)
        assert is_awaiting_manual_role_title(u) is True
        assert is_awaiting_manual_role_prompt(u) is False

    def test_set_title_transitions_to_prompt(self):
        u = uid()
        begin_manual_role_creation(u)
        set_manual_role_title(u, "My Role")
        assert is_awaiting_manual_role_title(u) is False
        assert is_awaiting_manual_role_prompt(u) is True
        assert get_manual_role_title(u) == "My Role"

    def test_set_prompt_stores_text(self):
        u = uid()
        set_manual_role_prompt(u, "Be a teacher")
        assert get_manual_role_prompt(u) == "Be a teacher"

    def test_finish_clears_awaiting_keeps_data(self):
        u = uid()
        begin_manual_role_creation(u)
        set_manual_role_title(u, "Title")
        set_manual_role_prompt(u, "Prompt")
        finish_manual_role_input(u)
        assert is_awaiting_manual_role_title(u) is False
        assert is_awaiting_manual_role_prompt(u) is False
        assert get_manual_role_title(u) == "Title"
        assert get_manual_role_prompt(u) == "Prompt"

    def test_clear_manual_role_resets_everything(self):
        u = uid()
        set_manual_role_title(u, "X")
        set_manual_role_prompt(u, "Y")
        clear_manual_role_state(u)
        assert get_manual_role_title(u) == ""
        assert get_manual_role_prompt(u) == ""
        assert is_awaiting_manual_role_title(u) is False


# ═══════════════════════════════════════════════════════════════════════════════
# Last message (retry)
# ═══════════════════════════════════════════════════════════════════════════════


class TestLastMessage:
    def test_default_is_none(self):
        u = uid()
        assert get_last_sent_message(u) is None

    def test_set_and_get(self):
        u = uid()
        set_last_sent_message(u, "Hello world")
        assert get_last_sent_message(u) == "Hello world"


# ═══════════════════════════════════════════════════════════════════════════════
# Public API helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestPublicAPI:
    def test_get_user_state_returns_user_state(self):
        s = get_user_state(uid())
        assert isinstance(s, UserState)

    def test_get_user_lock_returns_lock(self):
        import asyncio

        lock = get_user_lock(uid())
        assert isinstance(lock, asyncio.Lock)

    def test_same_user_gets_same_state(self):
        u = uid()
        s1 = get_user_state(u)
        s2 = get_user_state(u)
        assert s1 is s2

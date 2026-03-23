"""Tests for LTM consolidation debounce gate.

Validates:
1. First call returns False (initializes state).
2. Calls 2-19 return False (under message gate).
3. 20th call returns True (message gate triggered).
4. Time gate override: triggers before 20 messages if 15 min elapsed.
5. State reset works for testing.
6. Different users have independent state.
"""

import sys
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ── Mock all transitive deps of memory_consolidation before import ────
for mod_name in (
    "google", "google.generativeai",
    "app.database", "app.repos.db_helpers",
):
    sys.modules.setdefault(mod_name, MagicMock())

from app.repos.memory_consolidation import (
    _MSG_GATE,
    _TIME_GATE,
    reset_consolidation_state,
    should_check_consolidation,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset consolidation state before each test."""
    reset_consolidation_state()
    yield
    reset_consolidation_state()


class TestConsolidationDebounce:
    """Test the should_check_consolidation gate."""

    def test_first_call_returns_false(self):
        """First call initializes state and returns False."""
        assert should_check_consolidation(42) is False

    def test_under_msg_gate_returns_false(self):
        """Calls 1 through MSG_GATE-1 should all return False."""
        for _ in range(_MSG_GATE - 1):
            assert should_check_consolidation(42) is False

    def test_msg_gate_triggers_on_nth_call(self):
        """The MSG_GATE-th call should return True."""
        for _ in range(_MSG_GATE - 1):
            should_check_consolidation(42)

        # This is the MSG_GATE-th call
        assert should_check_consolidation(42) is True

    def test_resets_after_trigger(self):
        """After triggering, counter resets and next call returns False."""
        for _ in range(_MSG_GATE - 1):
            should_check_consolidation(42)
        assert should_check_consolidation(42) is True  # triggers

        # Next call should be False (counter reset)
        assert should_check_consolidation(42) is False

    def test_time_gate_override(self):
        """If 15 minutes elapsed, should trigger even with few messages."""
        should_check_consolidation(42)  # Initialize

        # Mock time to advance past TIME_GATE
        with patch("app.repos.memory_consolidation.time") as mock_time:
            # Simulate 16 minutes later
            mock_time.monotonic.return_value = time.monotonic() + _TIME_GATE + 60
            assert should_check_consolidation(42) is True

    def test_independent_user_state(self):
        """Different users should have independent counters."""
        # User 1: 19 calls
        for _ in range(_MSG_GATE - 1):
            should_check_consolidation(1)

        # User 2: only 1 call
        should_check_consolidation(2)

        # User 1's 20th should trigger
        assert should_check_consolidation(1) is True
        # User 2's 2nd should not
        assert should_check_consolidation(2) is False

    def test_reset_single_user(self):
        """reset_consolidation_state(uid) clears only that user."""
        for _ in range(5):
            should_check_consolidation(1)
            should_check_consolidation(2)

        reset_consolidation_state(1)

        # User 1 reset — first call is False
        assert should_check_consolidation(1) is False
        # User 2 — still at count 6, should not trigger yet
        assert should_check_consolidation(2) is False

    def test_reset_all_users(self):
        """reset_consolidation_state() clears all users."""
        for _ in range(5):
            should_check_consolidation(1)
            should_check_consolidation(2)

        reset_consolidation_state()

        assert should_check_consolidation(1) is False
        assert should_check_consolidation(2) is False

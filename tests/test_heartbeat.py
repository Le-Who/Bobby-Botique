"""Tests for app.utils.heartbeat — global heartbeat event registry."""

import asyncio

import pytest

from app.utils.heartbeat import (
    _HEARTBEAT_EVENTS,
    register_heartbeat,
    stop_heartbeat,
    unregister_heartbeat,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure heartbeat registry is clean for each test."""
    _HEARTBEAT_EVENTS.clear()
    yield
    _HEARTBEAT_EVENTS.clear()


class TestHeartbeatRegistry:
    """Heartbeat registry manages asyncio.Event objects keyed by message_id."""

    def test_register_stores_event(self):
        event = asyncio.Event()
        register_heartbeat(42, event)
        assert 42 in _HEARTBEAT_EVENTS
        assert _HEARTBEAT_EVENTS[42] is event

    def test_stop_sets_event_and_removes(self):
        event = asyncio.Event()
        register_heartbeat(42, event)
        stop_heartbeat(42)
        assert event.is_set()
        assert 42 not in _HEARTBEAT_EVENTS

    def test_stop_nonexistent_is_noop(self):
        # Should not raise
        stop_heartbeat(999)

    def test_stop_already_set_event(self):
        event = asyncio.Event()
        event.set()
        register_heartbeat(42, event)
        # Stopping an already-set event should still remove it
        stop_heartbeat(42)
        assert 42 not in _HEARTBEAT_EVENTS

    def test_unregister_removes_without_setting(self):
        event = asyncio.Event()
        register_heartbeat(42, event)
        unregister_heartbeat(42)
        assert not event.is_set()
        assert 42 not in _HEARTBEAT_EVENTS

    def test_unregister_nonexistent_is_noop(self):
        unregister_heartbeat(999)

    def test_multiple_registrations(self):
        e1 = asyncio.Event()
        e2 = asyncio.Event()
        register_heartbeat(1, e1)
        register_heartbeat(2, e2)
        stop_heartbeat(1)
        assert e1.is_set()
        assert not e2.is_set()
        assert 2 in _HEARTBEAT_EVENTS

    def test_overwrite_registration(self):
        e1 = asyncio.Event()
        e2 = asyncio.Event()
        register_heartbeat(42, e1)
        register_heartbeat(42, e2)
        stop_heartbeat(42)
        assert e2.is_set()
        # e1 was overwritten, not stopped
        assert not e1.is_set()

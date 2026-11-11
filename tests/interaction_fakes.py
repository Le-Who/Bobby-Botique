"""Synthetic durable privacy boundary for offline role/Tarot handler tests."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock


def install_private_interaction_fakes(monkeypatch, *, chat_reader=False):
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", AsyncMock(return_value=7))
    if chat_reader:
        monkeypatch.setattr("app.repos.chats.get_user_chat", AsyncMock(return_value=SimpleNamespace(memory_epoch=7)))
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", AsyncMock(return_value=True))

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)

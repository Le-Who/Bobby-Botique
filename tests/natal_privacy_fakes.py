"""Database-boundary fakes for the real natal epoch and lease workflow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock


def install_natal_privacy_database(monkeypatch):
    state = SimpleNamespace(epoch=100, blocked=False, has_chat=True, leases={}, captured_require_ltm=[])

    async def get_chat(user_id):
        return SimpleNamespace(memory_epoch=state.epoch, _has_persisted_chat=state.has_chat)

    async def ensure_generation(user_id, *, expected_epoch):
        if state.blocked or (expected_epoch is not None and expected_epoch != state.epoch):
            return None
        state.has_chat = True
        return state.epoch

    async def snapshot_current(user_id, expected_epoch, *, require_ltm):
        state.captured_require_ltm.append(require_ltm)
        return not state.blocked and expected_epoch == state.epoch

    async def acquire(user_id, expected_epoch, purpose, require_ltm, lease_id):
        state.captured_require_ltm.append(require_ltm)
        if state.blocked or expected_epoch != state.epoch:
            return False
        state.leases[lease_id] = (user_id, expected_epoch, purpose)
        return True

    async def release(user_id, lease_id):
        state.leases.pop(lease_id, None)

    state.get_chat = AsyncMock(side_effect=get_chat)
    state.ensure_generation = AsyncMock(side_effect=ensure_generation)
    monkeypatch.setattr("app.repos.chats.get_user_chat", state.get_chat)
    monkeypatch.setattr("app.repos.chats.ensure_chat_generation", state.ensure_generation)
    monkeypatch.setattr("app.repos.memory_consent.is_private_data_snapshot_current", snapshot_current)
    monkeypatch.setattr("app.repos.memory_consent._acquire_private_data_lease", acquire)
    monkeypatch.setattr("app.repos.memory_consent._release_private_data_lease", release)
    return state

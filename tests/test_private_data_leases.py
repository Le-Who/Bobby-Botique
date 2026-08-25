"""Durable cross-process privacy barriers for provider calls."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class _Connection:
    def __init__(self):
        self.transaction_contexts: list[_AsyncContext] = []
        self.execute = AsyncMock()

    def transaction(self):
        context = _AsyncContext(self)
        self.transaction_contexts.append(context)
        return context


class _Pool:
    def __init__(self, connection: _Connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def test_migration_069_uses_non_reusable_generations_and_durable_leases():
    migration = Path("scripts/migrations/069_durable_private_data_leases.sql").read_text(encoding="utf-8")
    normalized = " ".join(migration.lower().split())

    assert "create sequence if not exists memory_consent_epoch_seq" in normalized
    assert "alter column memory_epoch set default nextval" in normalized
    assert "private_data_blocked boolean not null default false" in normalized
    assert "new.memory_epoch := nextval" in normalized
    assert "create table if not exists private_data_leases" in normalized
    assert "memory_epoch bigint not null" in normalized
    assert "purpose text not null" in normalized
    assert "expires_at timestamptz not null" in normalized
    assert "references users" not in normalized
    assert "references chats" not in normalized
    assert "enable row level security" in normalized
    assert "private_data_leases_user_epoch_idx" in normalized
    assert "private_data_leases_expiry_idx" in normalized


def test_migration_runner_serializes_version_read_and_application():
    import inspect

    from app.db.migrations import _run_numbered_migrations_locked, run_migrations

    source = inspect.getsource(run_migrations)
    lock_position = source.index("pg_advisory_xact_lock")
    runner_position = source.index("_run_numbered_migrations_locked")
    locked_source = inspect.getsource(_run_numbered_migrations_locked)
    create_position = locked_source.index("CREATE TABLE IF NOT EXISTS schema_migrations")
    read_position = locked_source.index("SELECT version FROM schema_migrations")

    assert lock_position < runner_position
    assert create_position < read_position
    assert "conn=conn" in source


@pytest.mark.asyncio
async def test_missing_chat_snapshot_fails_closed():
    from app.repos import memory_consent

    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True)

    with (
        patch.object(memory_consent, "db_manager", manager),
        patch.object(memory_consent, "set_user_context", new_callable=AsyncMock),
        patch.object(memory_consent, "clear_user_context", new_callable=AsyncMock),
        patch.object(memory_consent, "db_query", new_callable=AsyncMock, return_value=[]),
    ):
        assert await memory_consent.is_private_data_snapshot_current(42, 9, require_ltm=True) is False


@pytest.mark.asyncio
async def test_stale_recreated_account_is_rejected_before_provider_use():
    """An old epoch cannot acquire a lease after account recreation."""
    from app.repos import memory

    provider = AsyncMock(return_value=[0.1, 0.2])

    @asynccontextmanager
    async def rejected_lease(*_args, **_kwargs):
        yield False

    with (
        patch("app.repos.memory_consent.private_data_lease", rejected_lease),
        patch.object(memory, "_get_embedding", provider),
    ):
        result = await memory.store_memory(
            42,
            "Private snapshot queued before delete and recreate",
            "key",
            expected_epoch=7,
        )

    assert result is None
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_ltm_disable_waits_for_only_invalidated_ltm_leases_after_commit():
    from app.repos import chats

    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True)
    wait = AsyncMock()

    async def query(sql, _params=(), *, conn=None):
        assert conn is connection
        assert connection.transaction_contexts[-1].exited is False
        return [{"memory_epoch": 12}]

    async def wait_after_commit(*args, **kwargs):
        assert connection.transaction_contexts[-1].exited is True
        return await wait(*args, **kwargs)

    with (
        patch.object(chats, "db_manager", manager),
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
        patch.object(chats, "db_query", side_effect=query),
        patch("app.repos.memory_consent.wait_for_private_data_leases", side_effect=wait_after_commit),
    ):
        epoch = await chats.set_ltm_enabled(42, False)

    assert epoch == 12
    connection.execute.assert_awaited_once_with("SELECT pg_advisory_xact_lock($1)", 42)
    wait.assert_awaited_once_with(42, before_epoch=12, ltm_only=True)


@pytest.mark.asyncio
async def test_summarizer_holds_account_scoped_lease_around_provider_call():
    from app.context import summarizer

    lease_events: list[str] = []
    provider = AsyncMock(return_value="durable summary")
    callback = AsyncMock()

    @asynccontextmanager
    async def allowed_lease(user_id, expected_epoch, *, purpose, require_ltm):
        assert (user_id, expected_epoch) == (42, 33)
        assert purpose == "conversation:summary"
        assert require_ltm is False
        lease_events.append("enter")
        try:
            yield True
        finally:
            lease_events.append("exit")

    async def provider_inside_lease(**_kwargs):
        assert lease_events == ["enter"]
        return await provider()

    with (
        patch("app.repos.memory_consent.private_data_lease", allowed_lease),
        patch("app.handlers.ai_core._get_ai_response_with_routing", side_effect=provider_inside_lease),
    ):
        await summarizer._run_llm_summarization(
            42,
            33,
            [{"role": "user", "parts": ["private history"]}],
            None,
            callback,
        )

    provider.assert_awaited_once()
    callback.assert_awaited_once_with("durable summary")
    assert lease_events == ["enter", "exit"]


@pytest.mark.asyncio
async def test_lease_heartbeat_loss_cancels_provider_owner_and_releases():
    import asyncio

    from app.repos import memory_consent

    provider_started = asyncio.Event()
    never = asyncio.Event()
    release = AsyncMock()

    async def worker():
        async with memory_consent.private_data_lease(
            42,
            9,
            purpose="ltm:test",
            require_ltm=True,
        ) as allowed:
            assert allowed is True
            provider_started.set()
            await never.wait()

    with (
        patch.object(memory_consent, "_acquire_private_data_lease", AsyncMock(return_value=True)),
        patch.object(memory_consent, "_renew_private_data_lease", AsyncMock(return_value=False)),
        patch.object(memory_consent, "_release_private_data_lease", release),
        patch.object(memory_consent, "_LEASE_HEARTBEAT_SECONDS", 0),
    ):
        task = asyncio.create_task(worker())
        await provider_started.wait()
        with pytest.raises(asyncio.CancelledError):
            await task

    release.assert_awaited_once()


def test_ltm_only_drain_never_sweeps_conversation_leases():
    import inspect

    from app.repos.memory_consent import wait_for_private_data_leases

    source = inspect.getsource(wait_for_private_data_leases)
    assert source.count("purpose LIKE 'ltm:%'") == 2

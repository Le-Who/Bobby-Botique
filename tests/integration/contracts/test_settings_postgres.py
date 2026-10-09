"""CORE-G04: PostgreSQL CAS, commit atomicity and pooled admin context."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.runtime_settings import store

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("existing", [False, True], ids=["absent-row", "existing-row"])
async def test_independent_store_cas_has_one_atomic_winner(contract_pool, db_conn, monkeypatch, existing):
    # Removing either INSERT conflict protection or UPDATE raw-value CAS loses this race.
    initial = store.RuntimeSettingsStore()
    if existing:
        await initial.set_value("process:seed", "preserved", expected_revision=0, actor="seed")
    expected = int(existing)
    ready = asyncio.Barrier(2)
    original_read = store.RuntimeSettingsStore._read
    read_pids = set()
    write_pids = set()

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def fetch(self, sql, *params):
            pid = await self.connection.fetchval("SELECT pg_backend_pid()")
            (read_pids if sql.startswith("SELECT value_data") else write_pids).add(pid)
            return await self.connection.fetch(sql, *params)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with contract_pool.acquire() as connection:
                yield Connection(connection)

    monkeypatch.setattr(store.db.db_manager, "pool", Pool())

    async def synchronized_read(self):
        result = await original_read(self)
        await ready.wait()
        return result

    monkeypatch.setattr(store.RuntimeSettingsStore, "_read", synchronized_read)
    instances = [store.RuntimeSettingsStore(), store.RuntimeSettingsStore()]
    outcomes = await asyncio.gather(
        *(
            instance.update_values(
                {"process:winner": actor, "prompt:paired": [actor]}, expected_revision=expected, actor=actor
            )
            for instance, actor in zip(instances, ("left", "right"), strict=True)
        ),
        return_exceptions=True,
    )
    assert len(read_pids) == len(write_pids) == 2
    winners = [outcome for outcome in outcomes if isinstance(outcome, store.SettingsSnapshot)]
    assert len(winners) == 1
    assert sum(isinstance(outcome, store.RevisionConflict) for outcome in outcomes) == 1
    raw = await db_conn.fetchval("SELECT value_data FROM global_settings WHERE key_name='runtime_controls:v1'")
    document = json.loads(raw)
    actor = document["actor"]
    assert actor in {"left", "right"}
    assert document["revision"] == expected + 1
    assert document["values"] == {
        **({"process:seed": "preserved"} if existing else {}),
        "process:winner": actor,
        "prompt:paired": [actor],
    }
    assert [entry["revision"] for entry in document["history"]] == list(range(expected + 1))
    assert document["history"][-1]["values"] == ({"process:seed": "preserved"} if existing else {})
    assert document["changed_keys"] == ["process:winner", "prompt:paired"]
    async with contract_pool.acquire() as connection:
        assert await connection.fetchval("SELECT current_setting('app.is_admin', true)") in {None, "", "false"}


async def test_canceled_settings_write_rolls_back_document_and_admin_context(contract_pool, db_conn, monkeypatch):
    # Cancellation after real INSERT but before transaction exit must undo the whole document.
    class CancelAfterWrite:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def fetch(self, sql, *params):
            rows = await self.connection.fetch(sql, *params)
            if sql.startswith("INSERT INTO global_settings"):
                raise asyncio.CancelledError
            return rows

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with contract_pool.acquire() as connection:
                yield CancelAfterWrite(connection)

    monkeypatch.setattr(store.db.db_manager, "pool", Pool())
    with pytest.raises(asyncio.CancelledError):
        await store.RuntimeSettingsStore().set_value("process:atomic", "aborted", expected_revision=0, actor="test")
    assert await db_conn.fetchval("SELECT count(*) FROM global_settings WHERE key_name='runtime_controls:v1'") == 0
    async with contract_pool.acquire() as connection:
        assert await connection.fetchval("SELECT current_setting('app.is_admin', true)") in {None, "", "false"}

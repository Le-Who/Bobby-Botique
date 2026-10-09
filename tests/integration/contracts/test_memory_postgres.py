"""ST-01/ST-02: durable privacy epochs and source-backed graph persistence."""

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from app.database import set_user_context
from app.repos import memory
from app.repos import memory_consent as consent
from app.repos.chats import replace_context_summary
from app.repos.memory_graph_writer import GraphEdgeCandidate, GraphMutationPlan, GraphNodeCandidate, write_graph

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = 83001


async def account(connection, user=USER):
    await connection.execute("INSERT INTO users(user_id) VALUES($1)", user)
    return await connection.fetchval(
        "INSERT INTO chats(user_id,ltm_enabled) VALUES($1,true) RETURNING memory_epoch", user
    )


async def source(connection, content, user=USER):
    return await connection.fetchval(
        "INSERT INTO long_term_memory(user_id,content,source_type) VALUES($1,$2,'contract') RETURNING id", user, content
    )


def plan(memory_id, description="old description", *, weight=0.4, core=False):
    return GraphMutationPlan(
        nodes=tuple(
            GraphNodeCandidate(name, "person", description, None, "identity", "profile", frozenset({memory_id}))
            for name in ("Ada", "Bob")
        ),
        edges=(GraphEdgeCandidate("Ada", "Bob", "knows", None, weight, core, frozenset({memory_id})),),
    )


async def embedded(epoch):
    return await memory._store_embedded_memory(
        user_id=USER,
        db_text_content="Controlled durable memory",
        embedding=[0.1] * 768,
        source_type="contract",
        metadata=None,
        ttl_days=0,
        expected_epoch=epoch,
        wing=None,
        room=None,
        hall_type=None,
    )


async def test_committed_barrier_rejects_old_provider_and_waits_only_ltm_lease(db_conn, monkeypatch):
    epoch = await account(db_conn)
    ltm_id, other_id = uuid4(), uuid4()
    assert await consent._acquire_private_data_lease(USER, epoch, "ltm:embedding", True, ltm_id)
    assert await consent._acquire_private_data_lease(USER, epoch, "voice:reply", False, other_id)
    drain_started = asyncio.Event()
    body_entered = asyncio.Event()
    original_wait = consent.wait_for_private_data_leases

    async def observed_wait(*args, **kwargs):
        drain_started.set()
        await original_wait(*args, **kwargs)

    monkeypatch.setattr(consent, "wait_for_private_data_leases", observed_wait)

    async def operation():
        async with consent.private_data_barrier(USER, is_admin=False, ltm_only=True) as (barrier, _):
            body_entered.set()
            return barrier

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(drain_started.wait(), 5)
        barrier, blocked = await db_conn.fetchrow(
            "SELECT memory_epoch,private_data_blocked FROM chats WHERE user_id=$1", USER
        )
        assert barrier > epoch and blocked is True
        assert not body_entered.is_set()
        assert await consent._acquire_private_data_lease(USER, epoch, "ltm:new", True, uuid4()) is False
        assert await consent._renew_private_data_lease(USER, ltm_id) is False
        assert await embedded(epoch) is None
        await consent._release_private_data_lease(USER, ltm_id)
        assert await asyncio.wait_for(task, 5) == barrier
        assert body_entered.is_set()
        assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE lease_id=$1", other_id) == 1
        assert await db_conn.fetchval("SELECT count(*) FROM long_term_memory") == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await consent._release_private_data_lease(USER, other_id)


async def test_lease_and_barrier_serialize_on_real_advisory_lock(contract_pool, db_conn, monkeypatch):
    epoch = await account(db_conn)
    lease_attempted = asyncio.Event()
    barrier_attempted = asyncio.Event()
    drain_entered = asyncio.Event()
    lease_id = uuid4()
    pids = set()
    original_wait = consent.wait_for_private_data_leases

    async def wait(*args, **kwargs):
        drain_entered.set()
        await original_wait(*args, **kwargs)

    monkeypatch.setattr(consent, "wait_for_private_data_leases", wait)

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def execute(self, sql, *params):
            if sql == "SELECT pg_advisory_xact_lock($1)":
                pids.add(await self.connection.fetchval("SELECT pg_backend_pid()"))
                task = asyncio.current_task()
                (lease_attempted if task.get_name() == "lease-contender" else barrier_attempted).set()
            return await self.connection.execute(sql, *params)

    class Pool:
        _closed = False

        @asynccontextmanager
        async def acquire(self):
            async with contract_pool.acquire() as connection:
                yield Connection(connection)

    monkeypatch.setattr(consent.db_manager, "pool", Pool())

    async def revoke():
        async with consent.private_data_barrier(USER, is_admin=False, ltm_only=True) as (barrier, _):
            return barrier

    lease_task = barrier_task = None
    try:
        async with contract_pool.acquire() as blocker, blocker.transaction():
            await blocker.execute("SELECT pg_advisory_xact_lock($1)", USER)
            lease_task = asyncio.create_task(
                consent._acquire_private_data_lease(USER, epoch, "ltm:race", True, lease_id), name="lease-contender"
            )
            barrier_task = asyncio.create_task(revoke(), name="barrier-contender")
            await asyncio.wait_for(asyncio.gather(lease_attempted.wait(), barrier_attempted.wait()), 5)
            assert len(pids) == 2
            assert not lease_task.done() and not barrier_task.done()
        await asyncio.wait_for(drain_entered.wait(), 5)
        acquired = await asyncio.wait_for(lease_task, 5)
        barrier, blocked = await db_conn.fetchrow(
            "SELECT memory_epoch,private_data_blocked FROM chats WHERE user_id=$1", USER
        )
        assert barrier > epoch and blocked is True
        if acquired:
            assert (
                await db_conn.fetchval("SELECT memory_epoch FROM private_data_leases WHERE lease_id=$1", lease_id)
                == epoch
            )
            await consent._release_private_data_lease(USER, lease_id)
        assert await asyncio.wait_for(barrier_task, 5) == barrier
        assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases") == 0
    finally:
        for task in (lease_task, barrier_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (lease_task, barrier_task) if task is not None), return_exceptions=True)


@pytest.mark.parametrize("supersede", [False, True], ids=["restore-exact-barrier", "preserve-superseding-E3"])
async def test_cancel_drain_compensates_only_its_own_generation(db_conn, monkeypatch, supersede):
    epoch = await account(db_conn)
    entered = asyncio.Event()
    never = asyncio.Event()

    async def paused_drain(*args, **kwargs):
        entered.set()
        await never.wait()

    monkeypatch.setattr(consent, "wait_for_private_data_leases", paused_drain)

    async def operation():
        async with consent.private_data_barrier(USER, is_admin=False, ltm_only=False):
            pytest.fail("Canceled drain must not start the destructive phase")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(entered.wait(), 5)
    barrier = await db_conn.fetchval("SELECT memory_epoch FROM chats WHERE user_id=$1", USER)
    if supersede:
        newer = await db_conn.fetchval(
            "UPDATE chats SET memory_epoch=nextval('memory_consent_epoch_seq'),ltm_enabled=false WHERE user_id=$1 RETURNING memory_epoch",
            USER,
        )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await db_conn.fetchrow(
        "SELECT memory_epoch,ltm_enabled,private_data_blocked FROM chats WHERE user_id=$1", USER
    )
    if supersede:
        assert tuple(row) == (newer, False, True)
    else:
        assert row["memory_epoch"] > barrier > epoch
        assert row["ltm_enabled"] is True and row["private_data_blocked"] is False
        assert await embedded(epoch) is None
        assert await embedded(row["memory_epoch"]) is not None


@pytest.mark.parametrize("supersede", [False, True], ids=["restore-exact-barrier", "preserve-superseding-E3"])
async def test_error_drain_compensates_only_its_own_generation(contract_pool, db_conn, monkeypatch, supersede):
    epoch = await account(db_conn)
    retained_memory = await source(db_conn, "Controlled retained source during failed drain")
    active_lease, expired_lease = uuid4(), uuid4()
    assert await consent._acquire_private_data_lease(USER, epoch, "ltm:active-drain", True, active_lease)
    assert await consent._acquire_private_data_lease(USER, epoch, "ltm:expired-drain", True, expired_lease)
    await db_conn.execute(
        "UPDATE private_data_leases SET expires_at=now()-interval '1 second' WHERE lease_id=$1", expired_lease
    )
    lease_rows = await db_conn.fetch(
        "SELECT lease_id,memory_epoch,purpose,expires_at FROM private_data_leases ORDER BY lease_id"
    )
    observer_pid = await db_conn.fetchval("SELECT pg_backend_pid()")
    drain_pids = set()
    drain_queries = []
    drain_entered = asyncio.Event()
    fail_drain = asyncio.Event()
    failure = RuntimeError("Controlled error after the real lease-drain query")
    phase_calls = []
    provider_calls = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def fetch(self, sql, *params):
            result = await self.connection.fetch(sql, *params)
            if "AS active_count" in sql and asyncio.current_task().get_name() == "failing-consent-drain":
                # The real wait loop deleted expired leases and counted the
                # remaining live lease in its real PostgreSQL transaction.
                assert result[0]["active_count"] == 1
                assert (
                    await self.connection.fetchval(
                        "SELECT count(*) FROM private_data_leases WHERE lease_id=$1", expired_lease
                    )
                    == 0
                )
                drain_pids.add(await self.connection.fetchval("SELECT pg_backend_pid()"))
                drain_queries.append(params)
                drain_entered.set()
                await fail_drain.wait()
                raise failure
            return result

    class Pool:
        _closed = False

        @asynccontextmanager
        async def acquire(self):
            async with contract_pool.acquire() as connection:
                yield Connection(connection)

    async def controlled_embedding(*args):
        provider_calls.append("embedding")
        assert (
            await db_conn.fetchval(
                "SELECT count(*) FROM private_data_leases WHERE user_id=$1 AND purpose='ltm:store_embedding'",
                USER,
            )
            == 1
        )
        return [0.1] * 768

    monkeypatch.setattr(consent.db_manager, "pool", Pool())
    monkeypatch.setattr(memory, "_get_embedding", controlled_embedding)

    async def operation():
        async with consent.private_data_barrier(USER, is_admin=False, ltm_only=False):
            phase_calls.append("destructive-delete")
            await db_conn.execute("DELETE FROM long_term_memory WHERE user_id=$1", USER)

    task = asyncio.create_task(operation(), name="failing-consent-drain")
    try:
        await asyncio.wait_for(drain_entered.wait(), 5)
        barrier_row = await db_conn.fetchrow(
            "SELECT memory_epoch,ltm_enabled,private_data_blocked FROM chats WHERE user_id=$1", USER
        )
        barrier = barrier_row["memory_epoch"]
        assert barrier > epoch and tuple(barrier_row) == (barrier, True, True)
        assert drain_pids and observer_pid not in drain_pids
        assert drain_queries == [(USER, barrier, False)]
        assert not task.done() and phase_calls == []
        # An independent connection still sees the expired row: the drain's
        # preceding DELETE has not committed and must roll back on the error.
        assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE lease_id=$1", expired_lease) == 1
        assert await consent._acquire_private_data_lease(USER, epoch, "ltm:blocked-old", True, uuid4()) is False
        assert await consent._renew_private_data_lease(USER, active_lease) is False
        if supersede:
            newer = await db_conn.fetchval(
                "UPDATE chats SET memory_epoch=nextval('memory_consent_epoch_seq'),ltm_enabled=false WHERE user_id=$1 RETURNING memory_epoch",
                USER,
            )
            assert newer > barrier
        fail_drain.set()
        with pytest.raises(RuntimeError, match="Controlled error after the real lease-drain query") as raised:
            await asyncio.wait_for(task, 5)
        assert raised.value is failure, "compensation must propagate the original drain error"
        row = await db_conn.fetchrow(
            "SELECT memory_epoch,ltm_enabled,private_data_blocked FROM chats WHERE user_id=$1", USER
        )
        if supersede:
            assert tuple(row) == (newer, False, True), "the old failure must not restore or unblock superseding E3"
        else:
            assert row["memory_epoch"] > barrier
            assert row["ltm_enabled"] is True and row["private_data_blocked"] is False
        assert phase_calls == []
        assert await db_conn.fetchval("SELECT id FROM long_term_memory WHERE id=$1", retained_memory) == retained_memory
        assert (
            await db_conn.fetch(
                "SELECT lease_id,memory_epoch,purpose,expires_at FROM private_data_leases ORDER BY lease_id"
            )
            == lease_rows
        )
        for stale in (epoch, barrier):
            assert (
                await memory.store_memory(USER, "Controlled stale provider input", "synthetic", expected_epoch=stale)
                is None
            )
            assert await embedded(stale) is None
        assert provider_calls == []
        if supersede:
            assert (
                await memory.store_memory(
                    USER, "Controlled disabled E3 provider input", "synthetic", expected_epoch=newer
                )
                is None
            )
            assert await embedded(newer) is None
            assert provider_calls == []
            assert await db_conn.fetchval("SELECT count(*) FROM long_term_memory") == 1
        else:
            assert (
                await memory.store_memory(
                    USER,
                    "Controlled fresh provider input after compensation",
                    "synthetic",
                    expected_epoch=row["memory_epoch"],
                )
                is not None
            )
            assert provider_calls == ["embedding"]
            assert await db_conn.fetchval("SELECT count(*) FROM long_term_memory") == 2
        assert (
            await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE purpose='ltm:store_embedding'") == 0
        )
    finally:
        fail_drain.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for lease_id in (active_lease, expired_lease):
            await consent._release_private_data_lease(USER, lease_id)


@pytest.mark.parametrize("after_commit", [False, True], ids=["rollback-before-commit", "cancel-after-real-commit"])
async def test_barrier_commit_cancellation_never_leaves_blocked_account(
    contract_pool, db_conn, monkeypatch, after_commit
):
    epoch = await account(db_conn)
    armed = True

    class Connection:
        def __init__(self, connection):
            self.connection = connection
            self.barrier_written = False

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def fetch(self, sql, *params):
            result = await self.connection.fetch(sql, *params)
            if "WITH barrier AS" in sql:
                self.barrier_written = True
            return result

        @asynccontextmanager
        async def transaction(self):
            nonlocal armed
            if after_commit:
                async with self.connection.transaction():
                    yield
                if self.barrier_written and armed:
                    armed = False
                    raise asyncio.CancelledError
            else:
                async with self.connection.transaction():
                    yield
                    if self.barrier_written and armed:
                        armed = False
                        raise asyncio.CancelledError

    class Pool:
        _closed = False

        @asynccontextmanager
        async def acquire(self):
            async with contract_pool.acquire() as connection:
                yield Connection(connection)

    monkeypatch.setattr(consent.db_manager, "pool", Pool())
    with pytest.raises(asyncio.CancelledError):
        async with consent.private_data_barrier(USER, is_admin=False, ltm_only=False):
            pytest.fail("Canceled commit must not enter phase two")
    row = await db_conn.fetchrow(
        "SELECT memory_epoch,ltm_enabled,private_data_blocked FROM chats WHERE user_id=$1", USER
    )
    assert row["private_data_blocked"] is False and row["ltm_enabled"] is True
    assert row["memory_epoch"] > epoch if after_commit else row["memory_epoch"] == epoch


async def test_erased_recreated_account_rejects_stale_provider_and_write(db_conn, monkeypatch):
    epoch = await account(db_conn)
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = []

    async def controlled_embedding(*args):
        provider_calls.append("started")
        provider_entered.set()
        await release_provider.wait()
        return [0.1] * 768

    monkeypatch.setattr(memory, "_get_embedding", controlled_embedding)
    task = asyncio.create_task(
        memory.store_memory(USER, "Controlled private content", "synthetic", expected_epoch=epoch)
    )
    try:
        await asyncio.wait_for(provider_entered.wait(), 5)
        # Exact persisted generation authority survives deletion even for the same Telegram ID.
        await db_conn.execute("DELETE FROM users WHERE user_id=$1", USER)
        newer = await account(db_conn)
        assert newer > epoch
        release_provider.set()
        assert await asyncio.wait_for(task, 5) is None
        assert await memory.store_memory(USER, "Another controlled memory", "synthetic", expected_epoch=epoch) is None
        assert provider_calls == ["started"]
        assert await embedded(epoch) is None
        assert (
            await replace_context_summary(USER, expected_summary=None, new_summary="stale", expected_epoch=epoch)
            is False
        )
        assert await embedded(newer) is not None
        assert await db_conn.fetchval("SELECT count(*) FROM long_term_memory") == 1
    finally:
        release_provider.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_source_delete_recomposes_attributes_then_deletes_last_graph(contract_pool, db_conn):
    await account(db_conn)
    old = await source(db_conn, "Older exact source")
    new = await source(db_conn, "Newer exact source")
    async with contract_pool.acquire() as connection, connection.transaction():
        await set_user_context(USER, conn=connection)
        result = await write_graph(connection, USER, plan(old))
        await connection.execute(
            "UPDATE memory_node_sources SET file_id='old-media',file_type='photo',created_at=now()-interval '1 day' WHERE memory_id=$1",
            old,
        )
    async with contract_pool.acquire() as connection, connection.transaction():
        await set_user_context(USER, conn=connection)
        await write_graph(connection, USER, plan(new, "new description", weight=0.9, core=True))
        await connection.execute(
            "UPDATE memory_node_sources SET file_id='new-media',file_type='video' WHERE memory_id=$1", new
        )
        await connection.execute("UPDATE memory_nodes SET file_id='new-media',file_type='video'")
    assert await db_conn.fetchval("SELECT weight FROM memory_edges") == pytest.approx(0.9)
    async with contract_pool.acquire() as connection, connection.transaction():
        await connection.execute("DELETE FROM long_term_memory WHERE id=$1", new)
        # Constraint triggers defer recomposition until the caller requests it or commits.
        assert await connection.fetchval("SELECT description FROM memory_nodes LIMIT 1") == "new description"
        await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        nodes = await connection.fetch("SELECT description,file_id,file_type FROM memory_nodes ORDER BY entity_name")
        assert [tuple(row) for row in nodes] == [("old description", "old-media", "photo")] * 2
        edge = await connection.fetchrow("SELECT weight,is_core,source_memory_ids FROM memory_edges")
        assert edge["weight"] == pytest.approx(0.4) and edge["is_core"] is False and edge["source_memory_ids"] == [old]
    assert await db_conn.fetchval("SELECT count(*) FROM memory_nodes") == 2
    await db_conn.execute("DELETE FROM long_term_memory WHERE id=$1", old)
    for table in ("memory_nodes", "memory_edges", "memory_node_sources", "memory_edge_sources"):
        assert await db_conn.fetchval(f"SELECT count(*) FROM {table}") == 0
    assert len(result.affected_node_ids) == 2


async def test_raw_source_removal_deletes_derivation_and_graph(db_conn, contract_pool):
    await account(db_conn)
    raw = await source(db_conn, "Raw exact source")
    derived = await source(db_conn, "Derived private fact")
    await db_conn.execute(
        "INSERT INTO memory_derivation_sources(derived_memory_id,source_memory_id,user_id) VALUES($1,$2,$3)",
        derived,
        raw,
        USER,
    )
    async with contract_pool.acquire() as connection, connection.transaction():
        await write_graph(connection, USER, plan(derived))
    await db_conn.execute("DELETE FROM long_term_memory WHERE id=$1", raw)
    for table in ("long_term_memory", "memory_derivation_sources", "memory_edges", "memory_nodes"):
        assert await db_conn.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_cross_tenant_provenance_failure_rolls_back_sources_and_graph(db_conn, contract_pool):
    await account(db_conn)
    await account(db_conn, USER + 1)
    foreign = await source(db_conn, "Foreign source", USER + 1)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        async with contract_pool.acquire() as connection, connection.transaction():
            owned = await source(connection, "Rolled-back fact")
            await write_graph(connection, USER, plan(owned))
            await connection.execute(
                "INSERT INTO memory_node_sources(node_id,memory_id,user_id,entity_type) SELECT id,$1,$2,'concept' FROM memory_nodes LIMIT 1",
                foreign,
                USER,
            )
    assert await db_conn.fetchval("SELECT count(*) FROM long_term_memory WHERE user_id=$1", USER) == 0
    for table in ("memory_nodes", "memory_edges", "memory_node_sources", "memory_edge_sources"):
        assert await db_conn.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_never_referenced_orphans_preserve_hour_grace(db_conn):
    await account(db_conn)
    await db_conn.execute(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type,updated_at) VALUES($1,'recent','concept',now()),($1,'stale','concept',now()-interval '61 minutes')",
        USER,
    )
    assert await db_conn.fetchval("SELECT delete_stale_orphaned_memory_nodes($1)", USER) == 1
    assert await db_conn.fetchval("SELECT entity_name FROM memory_nodes") == "recent"


async def test_applied_graph_event_does_not_claim_caller_commit(db_conn, contract_pool, caplog):
    await account(db_conn)
    caplog.set_level(logging.INFO, logger="app.observability")
    with pytest.raises(RuntimeError, match="caller abort"):
        async with contract_pool.acquire() as connection, connection.transaction():
            owned = await source(connection, "Uncommitted fact")
            await write_graph(connection, USER, plan(owned))
            assert await db_conn.fetchval("SELECT count(*) FROM memory_nodes") == 0
            raise RuntimeError("caller abort")
    finished = [
        record for record in caplog.records if getattr(record, "_event_name", "") == "memory.graph_write_finished"
    ]
    assert len(finished) == 1 and finished[0].outcome == "applied"
    for table in ("long_term_memory", "memory_nodes", "memory_edges", "memory_node_sources", "memory_edge_sources"):
        assert await db_conn.fetchval(f"SELECT count(*) FROM {table}") == 0

"""Committed PostgreSQL consent changes invalidate in-flight public recall."""

import asyncio
from uuid import uuid4

import pytest

from app.repos import memory, memory_consent

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
USER = 83017
EMBEDDING = [0.1] * 768
EMBEDDING_SQL = "[" + ",".join("0.1" for _ in range(768)) + "]"


async def _seed(connection, *, blocked=False):
    await connection.execute("INSERT INTO users(user_id) VALUES($1)", USER)
    epoch = await connection.fetchval(
        "INSERT INTO chats(user_id,ltm_enabled,private_data_blocked) VALUES($1,true,$2) RETURNING memory_epoch",
        USER,
        blocked,
    )
    memory_id = await connection.fetchval(
        """INSERT INTO long_term_memory(user_id,content,source_type,embedding)
        VALUES($1,'Synthetic current-generation fact','contract',$2::halfvec) RETURNING id""",
        USER,
        EMBEDDING_SQL,
    )
    return epoch, memory_id


@pytest.mark.parametrize("change", ["disable_enable", "blocked_same_epoch"])
async def test_public_recall_rejects_committed_change_after_embedding_started(
    contract_pool, db_conn, monkeypatch, change
):
    """Real SQL must reject stale/blocked consent before querying private rows."""
    epoch, _ = await _seed(db_conn)
    embedding_started = asyncio.Event()
    resume_embedding = asyncio.Event()
    embedding_calls = []
    private_reads = []
    original_query = memory.db_query

    async def embedding(text, _key, *, task_type):
        embedding_calls.append((text, task_type))
        embedding_started.set()
        await resume_embedding.wait()
        return EMBEDDING

    async def observed_query(sql, params=(), **kwargs):
        if "FROM long_term_memory" in sql:
            private_reads.append(sql)
        return await original_query(sql, params, **kwargs)

    monkeypatch.setattr(memory, "_get_embedding", embedding)
    monkeypatch.setattr(memory, "_trgm_available", False)
    monkeypatch.setattr(memory, "db_query", observed_query)
    task = asyncio.create_task(memory.search_memories(USER, "synthetic query", "synthetic-key", expected_epoch=epoch))
    try:
        await asyncio.wait_for(embedding_started.wait(), 5)
        assert await db_conn.fetchval("SELECT memory_epoch FROM private_data_leases WHERE user_id=$1", USER) == epoch
        observer_pid = await db_conn.fetchval("SELECT pg_backend_pid()")
        async with contract_pool.acquire() as other:
            assert await other.fetchval("SELECT pg_backend_pid()") != observer_pid
            if change == "disable_enable":
                async with other.transaction():
                    invalidated = await other.fetchval(
                        "UPDATE chats SET ltm_enabled=false WHERE user_id=$1 RETURNING memory_epoch", USER
                    )
                assert invalidated > epoch
                async with other.transaction():
                    newer = await other.fetchval(
                        """UPDATE chats SET ltm_enabled=true,memory_epoch=nextval('memory_consent_epoch_seq')
                        WHERE user_id=$1 RETURNING memory_epoch""",
                        USER,
                    )
                    await other.execute("DELETE FROM long_term_memory WHERE user_id=$1", USER)
                    await other.execute(
                        """INSERT INTO long_term_memory(user_id,content,source_type,embedding)
                        VALUES($1,'Synthetic newly enabled generation fact','contract',$2::halfvec)""",
                        USER,
                        EMBEDDING_SQL,
                    )
                assert newer > invalidated
                assert await db_conn.fetchval("SELECT ltm_enabled FROM chats WHERE user_id=$1", USER) is True
            else:
                async with other.transaction():
                    unchanged = await other.fetchval(
                        "UPDATE chats SET private_data_blocked=true WHERE user_id=$1 RETURNING memory_epoch", USER
                    )
                assert unchanged == epoch
        # The active operation still owns E1's lease after a real E3 commit.
        assert await db_conn.fetchval("SELECT memory_epoch FROM private_data_leases WHERE user_id=$1", USER) == epoch
        resume_embedding.set()
        assert await asyncio.wait_for(task, 5) == []
        assert private_reads == []
        assert len(embedding_calls) == 1
        assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE user_id=$1", USER) == 0
    finally:
        resume_embedding.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_public_recall_same_epoch_returns_the_real_private_row(db_conn, monkeypatch):
    epoch, memory_id = await _seed(db_conn)
    embedded_epochs = []

    async def embedding(_text, _key, *, task_type):
        assert task_type == "RETRIEVAL_QUERY"
        embedded_epochs.append(
            await db_conn.fetchval("SELECT memory_epoch FROM private_data_leases WHERE user_id=$1", USER)
        )
        return EMBEDDING

    monkeypatch.setattr(memory, "_get_embedding", embedding)
    monkeypatch.setattr(memory, "_trgm_available", False)
    result = await memory.search_memories(USER, "synthetic query", "synthetic-key", expected_epoch=epoch)
    assert [row["id"] for row in result] == [memory_id]
    assert result[0]["content"] == "Synthetic current-generation fact"
    assert result[0]["similarity"] > 0.99
    assert embedded_epochs == [epoch]
    assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE user_id=$1", USER) == 0


async def test_blocked_account_rejects_public_recall_before_embedding(db_conn, monkeypatch):
    epoch, _ = await _seed(db_conn, blocked=True)
    embedding_calls = []

    async def embedding(*_args, **_kwargs):
        embedding_calls.append(True)
        return EMBEDDING

    monkeypatch.setattr(memory, "_get_embedding", embedding)
    assert await memory.search_memories(USER, "synthetic query", "synthetic-key", expected_epoch=epoch) == []
    assert embedding_calls == []
    assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE user_id=$1", USER) == 0


async def test_canceled_owner_finishes_real_lease_release_before_exit(db_conn, monkeypatch):
    """Cancellation cannot leave the owner's real row for TTL-based drainage."""
    epoch, _ = await _seed(db_conn)
    foreign_id, newer_id = uuid4(), uuid4()
    await db_conn.executemany(
        """INSERT INTO private_data_leases(lease_id,user_id,memory_epoch,purpose,expires_at)
        VALUES($1,$2,$3,'ltm:synthetic-other',now()+interval '1 minute')""",
        [(foreign_id, USER + 1, epoch), (newer_id, USER, epoch + 100)],
    )
    owner_entered = asyncio.Event()
    never = asyncio.Event()
    release_started = asyncio.Event()
    release_may_finish = asyncio.Event()
    release_finished = asyncio.Event()
    owned_lease_ids = []
    original_query = memory_consent.db_query

    async def observed_query(sql, params=(), **kwargs):
        if "DELETE FROM public.private_data_leases WHERE user_id = $1 AND lease_id = $2" in sql:
            owned_lease_ids.append(params[1])
            release_started.set()
            await release_may_finish.wait()
            result = await original_query(sql, params, **kwargs)
            release_finished.set()
            return result
        return await original_query(sql, params, **kwargs)

    async def owner():
        async with memory_consent.private_data_lease(
            USER, epoch, purpose="ltm:cleanup-contract", require_ltm=True
        ) as allowed:
            assert allowed
            owner_entered.set()
            await never.wait()

    async def checkpoint():
        reached = asyncio.Event()
        asyncio.get_running_loop().call_soon(reached.set)
        await reached.wait()

    monkeypatch.setattr(memory_consent, "db_query", observed_query)
    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(owner_entered.wait(), 5)
        task.cancel("initial cancellation")
        await asyncio.wait_for(release_started.wait(), 5)
        task.cancel("second cancellation during SQL release")
        await checkpoint()
        await checkpoint()
        assert not task.done()
        assert (
            await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE lease_id=$1", owned_lease_ids[0])
            == 1
        )
        task.cancel("third cancellation during SQL release")
        await checkpoint()
        assert not task.done()
        release_may_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert release_finished.is_set() and task.cancelled()
        assert len(owned_lease_ids) == 1
        assert (
            await db_conn.fetchval("SELECT count(*) FROM private_data_leases WHERE lease_id=$1", owned_lease_ids[0])
            == 0
        )
        assert set(await db_conn.fetch("SELECT lease_id FROM private_data_leases")) == {(foreign_id,), (newer_id,)}
        await asyncio.wait_for(
            memory_consent.wait_for_private_data_leases(USER, before_epoch=epoch + 1, ltm_only=True), 5
        )
        assert await db_conn.fetchval("SELECT count(*) FROM private_data_leases") == 2
    finally:
        release_may_finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

"""ST-08: executable legacy upgrades and CLI/startup coordination."""

import asyncio
import importlib.util
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import migrations as startup
from app.db.migration_manifest import discover_migration_files, migration_version

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "scripts/migrations"


async def legacy_baseline(connection):
    # This connection points only at the generated clone, never the source DB.
    assert (await connection.fetchval("SELECT current_database()")).startswith("gemaibot_test_contract_")
    await connection.execute(
        "DROP SCHEMA public CASCADE; DROP SCHEMA IF EXISTS extensions CASCADE; CREATE SCHEMA public"
    )
    for path in discover_migration_files(MIGRATIONS):
        if migration_version(path) >= "065":
            break
        async with connection.transaction():
            await connection.execute(path.read_text(encoding="utf-8"))


async def apply(connection, number):
    path = next(path for path in discover_migration_files(MIGRATIONS) if migration_version(path) == number)
    async with connection.transaction():
        await connection.execute(path.read_text(encoding="utf-8"))


async def test_legacy_double_encoded_and_scalar_trivia_upgrade_preserves_cutoff(db_conn):
    await legacy_baseline(db_conn)
    await db_conn.execute("INSERT INTO users(user_id) VALUES(85001)")
    payload = [
        {
            "id": 1,
            "topic": "Science",
            "question": "Legacy controlled question?",
            "options": ["a", "b", "c", "d"],
            "correct_index": 0,
            "explanation": "Preserved explanation",
        }
    ]
    await db_conn.execute(
        "INSERT INTO daily_trivia_puzzles(puzzle_date,questions,super_questions,status) VALUES('2026-08-02',$1,$2,'ready'),('2026-08-03',$3,$4,'draft'),('2026-08-04',$5,$6,'draft')",
        json.dumps(payload),
        json.dumps(payload),
        42,
        {},
        "null",
        False,
    )
    await db_conn.execute(
        "INSERT INTO daily_trivia_results(user_id,puzzle_date,final_score,answers) VALUES(85001,'2026-08-02',73,$1),(85001,'2026-08-03',91,'[]')",
        [{"answer": 0}],
    )
    await db_conn.execute(
        "INSERT INTO daily_trivia_super_results(user_id,puzzle_date,delta_score) VALUES(85001,'2026-08-02',17),(85001,'2026-08-03',20)"
    )
    await db_conn.execute(
        "INSERT INTO daily_trivia_used_keys(object_norm,subobject_norm,used_at) VALUES('old','preserved','2026-08-02'),('new','removed','2026-08-03')"
    )
    before = await db_conn.fetchrow("SELECT * FROM daily_trivia_results WHERE puzzle_date='2026-08-02'")
    before_super = await db_conn.fetchrow("SELECT * FROM daily_trivia_super_results WHERE puzzle_date='2026-08-02'")
    await apply(db_conn, "065")
    assert (
        await db_conn.fetchval("SELECT questions FROM daily_trivia_puzzles WHERE puzzle_date='2026-08-02'") == payload
    )
    assert await db_conn.fetchval("SELECT questions FROM daily_trivia_puzzles WHERE puzzle_date='2026-08-03'") == 42
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_question_occurrences") == 2
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_facts") == 1
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_puzzle_revisions") == 3
    assert (
        await db_conn.fetchval("SELECT puzzle_revision_id FROM daily_trivia_results WHERE puzzle_date='2026-08-02'")
        is None
    )
    await apply(db_conn, "066")
    after = dict(await db_conn.fetchrow("SELECT * FROM daily_trivia_results"))
    assert {key: after[key] for key in before.keys()} == dict(before)
    after_super = dict(await db_conn.fetchrow("SELECT * FROM daily_trivia_super_results"))
    assert {key: after_super[key] for key in before_super.keys()} == dict(before_super)
    assert await db_conn.fetchval("SELECT array_agg(puzzle_date) FROM daily_trivia_puzzles") == [date(2026, 8, 2)]
    assert await db_conn.fetchval("SELECT count(*) FROM daily_trivia_question_occurrences") == 2
    assert await db_conn.fetchval("SELECT subobject_norm FROM daily_trivia_used_keys") == "preserved"


async def test_legacy_uuid_graph_upgrade_preserves_relationships_and_provenance(db_conn):
    await legacy_baseline(db_conn)
    await db_conn.execute("DROP TABLE memory_edges; DROP TABLE memory_nodes")
    baseline = (MIGRATIONS / "024b_add_missing_graph_tables.sql").read_text(encoding="utf-8")
    baseline = baseline.replace("id BIGSERIAL PRIMARY KEY", "id UUID PRIMARY KEY DEFAULT gen_random_uuid()")
    baseline = baseline.replace("source_node BIGINT", "source_node UUID").replace(
        "target_node BIGINT", "target_node UUID"
    )
    await db_conn.execute(baseline)
    for number in ("025", "026", "026b", "027", "028", "029", "030", "032"):
        await apply(db_conn, number)
    await db_conn.execute("INSERT INTO users(user_id) VALUES(85001),(85002)")
    await db_conn.execute("INSERT INTO chats(user_id) VALUES(85001)")
    memory_id = await db_conn.fetchval(
        "INSERT INTO long_term_memory(user_id,content) VALUES(85001,'Preserved raw source') RETURNING id"
    )
    first = await db_conn.fetchval(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type,description) VALUES(85001,'Ada','person','Preserved Ada') RETURNING id"
    )
    second = await db_conn.fetchval(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type,description) VALUES(85001,'Bob','person','Preserved Bob') RETURNING id"
    )
    foreign = await db_conn.fetchval(
        "INSERT INTO memory_nodes(user_id,entity_name,entity_type) VALUES(85002,'Foreign','person') RETURNING id"
    )
    await db_conn.execute(
        "INSERT INTO memory_edges(user_id,source_node,target_node,predicate,weight,is_core,source_memory_ids) VALUES(85001,$1,$2,'knows',.7,true,$4),(85001,$1,$3,'invalid foreign',.9,false,'{}')",
        first,
        second,
        foreign,
        [memory_id],
    )
    await apply(db_conn, "067")
    columns = await db_conn.fetch(
        "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_name IN ('memory_nodes','memory_edges') AND column_name IN ('id','source_node','target_node')"
    )
    assert len(columns) == 4 and all(row["data_type"] == "bigint" for row in columns)
    edge = await db_conn.fetchrow(
        "SELECT s.entity_name AS source,t.entity_name AS target,e.predicate,e.weight,e.is_core,e.source_memory_ids FROM memory_edges e JOIN memory_nodes s ON s.id=e.source_node JOIN memory_nodes t ON t.id=e.target_node"
    )
    assert edge["source"] == "Ada" and edge["target"] == "Bob" and edge["predicate"] == "knows"
    assert edge["weight"] == pytest.approx(0.7) and edge["is_core"] is True and edge["source_memory_ids"] == [memory_id]
    assert await db_conn.fetchval("SELECT count(*) FROM memory_edges") == 1
    assert await db_conn.fetchval("SELECT count(*) FROM memory_edge_sources WHERE memory_id=$1", memory_id) == 1
    assert await db_conn.fetchval("SELECT count(*) FROM memory_node_sources WHERE memory_id=$1", memory_id) == 2
    assert await db_conn.fetchval("SELECT content FROM long_term_memory") == "Preserved raw source"
    assert await db_conn.fetchval("SELECT description FROM memory_nodes WHERE entity_name='Ada'") == "Preserved Ada"
    assert (
        await db_conn.fetchval(
            "INSERT INTO memory_nodes(user_id,entity_name,entity_type) VALUES(85001,'new','concept') RETURNING id"
        )
        > 3
    )


async def test_cli_and_startup_apply_pending_migration_once(
    contract_pool, contract_database_url, db_conn, tmp_path, monkeypatch
):
    # A stale CLI pending list must not replay DDL already committed by startup.
    migration = tmp_path / "999_contract_once.sql"
    migration.write_text(
        "CREATE TABLE contract_apply_once(value integer); INSERT INTO contract_apply_once VALUES(1);", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("contract_migrate_cli", ROOT / "scripts/migrate.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "_discover_files", lambda: [migration])
    monkeypatch.setattr(startup, "discover_migration_files", lambda _: [migration])
    startup_read = asyncio.Event()
    cli_attempted = asyncio.Event()
    startup_done = asyncio.Event()
    pids = set()
    original_pending = cli._pending

    async def pending(connection):
        result = await original_pending(connection)
        cli_attempted.set()
        await startup_done.wait()
        return result

    monkeypatch.setattr(cli, "_pending", pending)

    async def query(sql, params=(), conn=None):
        if conn is not None:
            rows = await conn.fetch(sql, *params)
        else:
            async with contract_pool.acquire() as connection:
                rows = await connection.fetch(sql, *params)
        if sql == "SELECT version FROM schema_migrations ORDER BY version":
            pids.add(await conn.fetchval("SELECT pg_backend_pid()"))
            startup_read.set()
            await cli_attempted.wait()
        return rows

    class ObservedCLIConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def execute(self, sql, *params):
            if "pg_advisory_xact_lock" in sql:
                cli_attempted.set()
            return await self.connection.execute(sql, *params)

    original_connect = cli.asyncpg.connect

    async def connect(*args, **kwargs):
        connection = await original_connect(*args, **kwargs)
        pids.add(await connection.fetchval("SELECT pg_backend_pid()"))
        return ObservedCLIConnection(connection)

    monkeypatch.setattr(cli, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("DATABASE_URL", contract_database_url)

    async def run_startup():
        result = await startup.run_migrations(query, SimpleNamespace(pool=contract_pool))
        startup_done.set()
        return result

    first = asyncio.create_task(run_startup())
    second = None
    try:
        await asyncio.wait_for(startup_read.wait(), 5)
        second = asyncio.create_task(cli.main(SimpleNamespace(mode="apply")))
        startup_result, cli_result = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert startup_result.success and cli_result == 0
        assert len(pids) == 2
        assert await db_conn.fetchval("SELECT count(*) FROM contract_apply_once") == 1
        assert await db_conn.fetchval("SELECT count(*) FROM schema_migrations WHERE version='999'") == 1
        tracking = await db_conn.fetchrow("SELECT * FROM schema_migrations WHERE version='999'")
        assert await cli.main(SimpleNamespace(mode="apply")) == 0
        rerun = await startup.run_migrations(query, SimpleNamespace(pool=contract_pool))
        assert rerun.success and rerun.applied == []
        assert await db_conn.fetchrow("SELECT * FROM schema_migrations WHERE version='999'") == tracking
        assert await db_conn.fetchval("SELECT count(*) FROM contract_apply_once") == 1
    finally:
        first.cancel()
        if second is not None:
            second.cancel()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)

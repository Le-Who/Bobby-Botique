"""Focused contracts for the LTM schema and GraphRAG repository baseline."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.db import rls, schema
from app.repos import memory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts" / "migrations" / "067_harden_long_term_memory.sql"


@pytest.fixture(autouse=True)
def _allow_private_data_lease_boundary():
    @asynccontextmanager
    async def allowed(*_args, **_kwargs):
        yield True

    with (
        patch("app.repos.memory_consent.private_data_lease", allowed),
        patch(
            "app.repos.memory_consent.resolve_current_epoch",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        yield


class _AsyncContext:
    def __init__(self, value, *, on_enter=None, on_exit=None):
        self.value = value
        self.on_enter = on_enter
        self.on_exit = on_exit

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        if self.on_exit:
            self.on_exit()
        return False


class _FakeConnection:
    def __init__(self):
        self.in_transaction = False
        self.execute_calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(
            None,
            on_enter=lambda: setattr(self, "in_transaction", True),
            on_exit=lambda: setattr(self, "in_transaction", False),
        )

    async def execute(self, sql: str, *args):
        assert self.in_transaction, "repository SQL must execute inside the transaction"
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


def _fake_pool(conn):
    return SimpleNamespace(acquire=lambda: _AsyncContext(conn))


class _SequencePool:
    def __init__(self, connections):
        self._connections = iter(connections)
        self.acquired: list[_FakeConnection] = []

    def acquire(self):
        conn = next(self._connections)
        self.acquired.append(conn)
        return _AsyncContext(conn)


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_adds_epoch_consolidation_and_node_timestamps():
    sql = _migration_sql()

    assert "add column if not exists memory_epoch bigint not null default 0" in sql
    assert "add column if not exists consolidated_at timestamptz" in sql
    assert "memory_nodes" in sql
    assert "add column if not exists updated_at timestamptz" in sql


def test_migration_upgrades_legacy_uuid_graph_ids_before_bigint_provenance():
    sql = _migration_sql()

    compatibility_start = sql.index("migration_067_node_id_map")
    provenance_start = sql.index("create table if not exists memory_edge_sources")
    compatibility_sql = sql[compatibility_start:provenance_start]

    assert "atttypid = 'uuid'::regtype" in compatibility_sql
    assert "lock table public.memory_nodes, public.memory_edges in access exclusive mode" in compatibility_sql
    assert "alter column source_node type bigint" in compatibility_sql
    assert "alter column target_node type bigint" in compatibility_sql
    assert "alter column id type bigint" in compatibility_sql
    assert "migration_067_node_id_to_bigint" in compatibility_sql
    assert "migration_067_edge_id_to_bigint" in compatibility_sql
    assert "memory_nodes_id_seq" in compatibility_sql
    assert "memory_edges_id_seq" in compatibility_sql
    assert compatibility_start < provenance_start


def test_migration_normalizes_provenance_with_tenant_fks_and_backfill():
    sql = _migration_sql()

    assert "create table if not exists memory_edge_sources" in sql
    assert "edge_id bigint not null" in sql
    assert "memory_id bigint not null" in sql
    assert "user_id bigint not null" in sql
    assert "primary key (edge_id, memory_id)" in sql
    assert "foreign key (edge_id, user_id)" in sql
    assert "references memory_edges (id, user_id) on delete cascade" in sql
    assert "foreign key (memory_id, user_id)" in sql
    assert "references long_term_memory (id, user_id) on delete cascade" in sql
    assert "unnest" in sql and "source_memory_ids" in sql
    assert "on conflict (edge_id, memory_id) do nothing" in sql


def test_migration_tracks_mutable_graph_attributes_per_exact_source():
    sql = _migration_sql()

    assert "create table if not exists memory_node_sources" in sql
    assert "primary key (node_id, memory_id)" in sql
    assert "references memory_nodes (id, user_id) on delete cascade" in sql
    assert "references long_term_memory (id, user_id) on delete cascade" in sql
    for attribute in ("entity_type", "description", "embedding", "file_id", "file_type", "attributes_complete"):
        assert attribute in sql[sql.index("create table if not exists memory_node_sources") :]

    edge_sources = sql[sql.index("create table if not exists memory_edge_sources") :]
    for attribute in ("predicate", "predicate_embedding", "weight", "is_core", "attributes_complete"):
        assert attribute in edge_sources

    assert "recompute_memory_node_after_source_removal" in sql
    assert "recompute_memory_edge_after_source_removal" in sql
    assert "after delete on memory_node_sources" in sql
    assert "after delete on memory_edge_sources" in sql
    assert "bool_or" in sql
    assert "attributes_complete is false" in sql

    edge_recompute = sql[
        sql.index("create or replace function recompute_memory_edge_after_source_removal") : sql.index(
            "create or replace function delete_orphaned_memory_edge"
        )
    ]
    assert "max(weight)" in edge_recompute
    assert "bool_or(is_core)" in edge_recompute
    assert "source_memory_ids = live_source_ids" in edge_recompute
    assert "delete from memory_edges" in edge_recompute

    node_recompute = sql[
        sql.index("create or replace function recompute_memory_node_after_source_removal") : sql.index(
            "create or replace function delete_stale_orphaned_memory_nodes"
        )
    ]
    assert "description = null" in node_recompute
    assert "file_id = null" in node_recompute
    assert "order by created_at desc, memory_id desc" in node_recompute


def test_migration_enforces_tenant_owned_edge_endpoints_with_safe_validation():
    sql = _migration_sql()

    assert "idx_memory_nodes_id_user_id" in sql
    assert "on memory_nodes (id, user_id)" in sql
    assert "constraint memory_edges_source_tenant_fk" in sql
    assert "foreign key (source_node, user_id)" in sql
    assert "constraint memory_edges_target_tenant_fk" in sql
    assert "foreign key (target_node, user_id)" in sql
    assert "references memory_nodes (id, user_id) on delete cascade" in sql

    source_constraint = sql.index("add constraint memory_edges_source_tenant_fk")
    target_constraint = sql.index("add constraint memory_edges_target_tenant_fk")
    cleanup = sql.index("delete from memory_edges as edge", source_constraint)
    source_validation = sql.index("validate constraint memory_edges_source_tenant_fk", cleanup)
    target_validation = sql.index("validate constraint memory_edges_target_tenant_fk", cleanup)
    assert "not valid" in sql[source_constraint:cleanup]
    assert source_constraint < cleanup < source_validation
    assert target_constraint < cleanup < target_validation


def test_migration_tracks_raw_to_derived_memory_and_cascades_on_any_source_delete():
    sql = _migration_sql()

    assert "create table if not exists memory_derivation_sources" in sql
    assert "derived_memory_id bigint not null" in sql
    assert "source_memory_id bigint not null" in sql
    assert "primary key (derived_memory_id, source_memory_id)" in sql
    assert "foreign key (derived_memory_id, user_id)" in sql
    assert "foreign key (source_memory_id, user_id)" in sql
    assert sql.count("references long_term_memory (id, user_id) on delete cascade") >= 3
    assert "check (derived_memory_id <> source_memory_id)" in sql
    assert "after delete on memory_derivation_sources" in sql
    assert "delete from long_term_memory" in sql


def test_migration_deletes_edges_after_their_last_source_disappears():
    sql = _migration_sql()

    assert "create or replace function delete_orphaned_memory_edge" in sql
    assert "after delete on memory_edge_sources" in sql
    assert "deferrable initially deferred" in sql
    assert "not exists" in sql
    assert "delete from memory_edges" in sql
    assert "delete from memory_nodes" in sql
    assert "source_node" in sql
    assert "target_node" in sql

    trigger_sql = sql[sql.index("create or replace function delete_orphaned_memory_edge") :]
    assert "for update" in trigger_sql
    assert trigger_sql.index("for update") < trigger_sql.index("if not exists")


def test_migration_and_retention_cleanup_sweep_stale_never_linked_nodes():
    sql = _migration_sql()
    repository_source = (ROOT / "app" / "repos" / "memory.py").read_text(encoding="utf-8").lower()

    assert "create or replace function delete_stale_orphaned_memory_nodes" in sql
    assert "delete from memory_nodes" in sql
    assert "updated_at <" in sql
    assert "not exists" in sql
    assert "delete_stale_orphaned_memory_nodes" in repository_source

    sweep_sql = sql[sql.index("create or replace function delete_stale_orphaned_memory_nodes") :]
    edge_delete = sweep_sql.index("delete from memory_edges")
    node_delete = sweep_sql.index("delete from memory_nodes")
    assert "memory_edge_sources" in sweep_sql[:node_delete]
    assert edge_delete < node_delete


def test_migration_replaces_full_edge_uniqueness_with_current_edge_uniqueness():
    sql = _migration_sql()

    assert "drop index if exists idx_memory_edges_unique" in sql
    assert "create unique index if not exists idx_memory_edges_current_unique" in sql
    assert "on memory_edges (user_id, source_node, target_node, predicate)" in sql
    assert "where valid_to is null" in sql


def test_migration_has_retention_fk_indexes_and_non_forced_rls():
    sql = _migration_sql()

    for index_name in (
        "idx_ltm_user_created_at",
        "idx_ltm_expires_at",
        "idx_ltm_unconsolidated",
        "idx_memory_edges_source_node_fk",
        "idx_memory_edges_target_node_fk",
        "idx_memory_edge_sources_memory_user",
        "idx_memory_node_sources_memory_user",
        "idx_memory_derivation_sources_source_user",
    ):
        assert index_name in sql

    for table in (
        "long_term_memory",
        "memory_nodes",
        "memory_edges",
        "memory_edge_sources",
        "memory_node_sources",
        "memory_derivation_sources",
    ):
        assert f"alter table {table} enable row level security" in sql
        assert f"on {table}" in sql

    assert "force row level security" not in sql


def test_migration_invalidates_queued_writes_when_ltm_is_disabled():
    sql = _migration_sql()

    assert "before insert or update of ltm_enabled on chats" in sql
    assert "tg_op = 'insert'" in sql
    assert "new.ltm_enabled is false" in sql
    assert "greatest(new.memory_epoch, 1)" in sql
    assert "old.ltm_enabled is true" in sql
    assert "new.ltm_enabled is false" in sql
    assert "new.memory_epoch := old.memory_epoch + 1" in sql


def test_rls_config_includes_all_ltm_graph_tables():
    assert {
        "long_term_memory",
        "memory_nodes",
        "memory_edges",
        "memory_edge_sources",
        "memory_node_sources",
        "memory_derivation_sources",
    }.issubset(rls.RLS_CONFIG)


def test_schema_validation_requires_all_normalized_provenance_tables():
    assert {
        "memory_edge_sources",
        "memory_node_sources",
        "memory_derivation_sources",
    }.issubset(schema.EXPECTED_TABLES)


@pytest.mark.asyncio
async def test_rls_setup_does_not_stop_when_users_policy_already_exists():
    calls: list[tuple[str, tuple | None]] = []

    async def fake_query(sql, params=None):
        calls.append((sql, params))
        if "select policyname from pg_policies" in sql.lower():
            table = params[0]
            return [{"policyname": "users_policy"}] if table == "users" else []
        if "tablename = 'users'" in sql.lower():
            return [{"exists": 1}]
        return []

    await rls.setup_row_level_security(fake_query)

    statements = [sql for sql, _ in calls]
    assert any('ALTER TABLE "memory_nodes" ENABLE ROW LEVEL SECURITY' in sql for sql in statements)
    assert any('CREATE POLICY "memory_edges_user_policy"' in sql for sql in statements)
    assert any('CREATE POLICY "memory_edge_sources_user_policy"' in sql for sql in statements)
    assert any('CREATE POLICY "memory_node_sources_user_policy"' in sql for sql in statements)
    assert any('CREATE POLICY "memory_derivation_sources_user_policy"' in sql for sql in statements)


@pytest.mark.asyncio
async def test_direct_search_checks_read_consent_before_embedding():
    embedding = AsyncMock(return_value=[0.1])

    with (
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=False)),
        patch.object(memory, "_get_embedding", embedding),
    ):
        result = await memory.search_memories(42, "private query", "key")

    assert result == []
    embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_search_checks_read_consent_before_expansion_or_embedding():
    memory._current_retrieved_edge_ids.set((42, (999,)))
    expand = AsyncMock(return_value="expanded")
    vector_search = AsyncMock(return_value=[{"id": 1}])
    embedding = AsyncMock(return_value=[0.1])

    with (
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=False)),
        patch.object(memory, "_should_expand_query", return_value=True),
        patch.object(memory, "expand_query_with_llm", expand),
        patch.object(memory, "search_memories", vector_search),
        patch.object(memory, "_get_embedding", embedding),
    ):
        result = await memory.search_memories_with_graph(42, "private query", "key")

    assert result == ([], [], {})
    assert memory.get_current_retrieved_edge_ids(42) == []
    expand.assert_not_awaited()
    vector_search.assert_not_awaited()
    embedding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("consent_result", [[], RuntimeError("database unavailable")])
async def test_read_consent_is_fail_closed_for_missing_rows_and_database_errors(consent_result):
    conn = _FakeConnection()

    async def fake_query(sql, params=None, *, conn=None):
        if isinstance(consent_result, Exception):
            raise consent_result
        return consent_result

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(
            memory,
            "set_user_context",
            AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
        ),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        assert await memory._is_ltm_read_enabled(42) is False


def assert_in_transaction(conn):
    assert conn.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_row", "expected_epoch"),
    [
        ({"ltm_enabled": False, "memory_epoch": 4}, 4),
        ({"ltm_enabled": True, "memory_epoch": 5}, 4),
    ],
)
async def test_store_preflight_rejects_revoked_or_stale_consent_before_embedding(chat_row, expected_epoch):
    conn = _FakeConnection()
    embedding = AsyncMock(return_value=[0.1, 0.2])

    @asynccontextmanager
    async def consent_lease(_user_id, lease_epoch, **_kwargs):
        yield bool(chat_row["ltm_enabled"] and chat_row["memory_epoch"] == lease_epoch)

    with (
        patch("app.repos.memory_consent.private_data_lease", consent_lease),
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_get_embedding", embedding),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(return_value=[chat_row])),
    ):
        result = await memory.store_memory(
            42,
            "A sufficiently long private memory",
            "key",
            expected_epoch=expected_epoch,
        )

    assert result is None
    embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_preflight_helper_is_public_transactional_and_missing_row_fails_closed():
    conn = _FakeConnection()
    query_mock = AsyncMock(return_value=[])

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(
            memory,
            "set_user_context",
            AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
        ),
        patch.object(memory, "db_query", query_mock),
    ):
        allowed = await memory.is_ltm_write_enabled(42, expected_epoch=0)

    assert allowed is False


@pytest.mark.asyncio
async def test_direct_search_rechecks_consent_under_share_lock_before_ltm_query():
    conn = _FakeConnection()
    calls: list[tuple[str, bool]] = []

    async def fake_query(sql, params=None, *, conn=None):
        calls.append((sql, conn.in_transaction))
        if "FROM chats" in sql:
            return [{"ltm_enabled": False}]
        raise AssertionError("LTM rows must not be read after committed opt-out")

    with (
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "_check_trgm_available", AsyncMock(return_value=False)),
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(
            memory,
            "set_user_context",
            AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
        ),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        result = await memory.search_memories(42, "private query", "key")

    assert result == []
    assert len(calls) == 1
    consent_sql, in_transaction = calls[0]
    assert "FOR SHARE" in consent_sql
    assert in_transaction


@pytest.mark.asyncio
async def test_graph_rechecks_consent_under_share_lock_before_graph_query():
    conn = _FakeConnection()
    vector_memories = [{"id": 7, "content": "vector result"}]
    query_mock = AsyncMock(return_value=[{"ltm_enabled": False}])

    with (
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=vector_memories)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(
            memory,
            "set_user_context",
            AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
        ),
        patch.object(memory, "db_query", query_mock),
    ):
        result = await memory.search_memories_with_graph(42, "private query", "key")

    assert result == ([], [], {})
    assert query_mock.await_count == 1
    consent_sql = query_mock.await_args.args[0]
    assert "FROM chats" in consent_sql
    assert "FOR SHARE" in consent_sql


@pytest.mark.asyncio
async def test_graph_refreshes_consent_before_repeated_external_embedding():
    consent = AsyncMock(side_effect=[True, False])
    vector_search = AsyncMock(return_value=[{"id": 7, "content": "vector result"}])
    graph_embedding = AsyncMock(return_value=[0.1])

    with (
        patch.object(memory, "_is_ltm_read_enabled", consent),
        patch.object(memory, "_should_expand_query", return_value=False),
        patch.object(memory, "search_memories", vector_search),
        patch.object(memory, "_get_embedding", graph_embedding),
    ):
        result = await memory.search_memories_with_graph(42, "private query", "key")

    assert result == ([], [], {})
    assert consent.await_count == 2
    graph_embedding.assert_not_awaited()
    assert vector_search.await_args.kwargs.get("_consent_checked") is not True


@pytest.mark.asyncio
async def test_llm_judge_rechecks_consent_after_retrieval_before_external_call():
    generate_content = AsyncMock(return_value=SimpleNamespace(text="[]"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    with (
        patch.object(
            memory,
            "search_memories",
            AsyncMock(return_value=[{"id": 1, "content": "stored private fact"}]),
        ),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=False)),
        patch.object(memory, "get_cached_genai_client", return_value=client),
    ):
        result = await memory.search_memories_with_llm_judge(42, "query", "key")

    assert result == []
    generate_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_hybrid_search_includes_keyword_only_candidates_and_orders_by_penalized_score():
    conn = _FakeConnection()
    query_mock = AsyncMock(
        return_value=[
            {
                "id": 77,
                "content": "exact keyword candidate",
                "source_type": "conversation",
                "metadata": {},
                "created_at": None,
                "sim": 0.20,
                "rlhf_neg": 0,
                "rank_k": 1,
                "rrf_score": 0.016,
                "final_score": 0.016,
            }
        ]
    )

    with (
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "_check_trgm_available", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
    ):
        results = await memory.search_memories(42, "exact keyword", "key")

    sql = query_mock.await_args.args[0].lower()
    assert "full outer join keyword" in sql
    assert "coalesce(s.id, k.id)" in sql
    assert "or rank_k is not null" in sql
    assert "final_score" in sql
    assert "order by final_score desc" in sql
    assert results == [
        {
            "id": 77,
            "content": "exact keyword candidate",
            "similarity": pytest.approx(0.20),
            "source_type": "conversation",
            "created_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_graph_sql_failure_preserves_vector_memories_and_empty_passages():
    conn = _FakeConnection()
    vector_memories = [{"id": 7, "content": "vector result"}]

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=vector_memories)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=RuntimeError("graph unavailable"))),
    ):
        result = await memory.search_memories_with_graph(42, "short query", "key")

    assert result == (vector_memories, [], {})


@pytest.mark.asyncio
async def test_graph_retrieval_uses_bigint_ids_and_live_tenant_provenance():
    conn = _FakeConnection()
    query_mock = AsyncMock(
        side_effect=[
            [{"id": 11, "entity_name": "user", "entity_type": "person", "description": "", "sim": 0.9}],
            [
                {
                    "from_name": "user",
                    "predicate": "likes",
                    "to_name": "tea",
                    "weight": 1.0,
                    "is_core": False,
                    "hop": 1,
                    "edge_id": 22,
                    "source_memory_ids": [33],
                    "effective_weight": 1.0,
                }
            ],
            [{"id": 33, "content": "User likes tea"}],
            [],
        ]
    )

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=[])),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
    ):
        await memory.search_memories_with_graph(42, "short query", "key")

    sql_calls = [call.args[0] for call in query_mock.await_args_list]
    graph_sql = next(sql for sql in sql_calls if "WITH hop1" in sql or "WITH live_edge_sources" in sql)
    temporal_sql = next(sql for sql in sql_calls if "e.valid_to IS NOT NULL" in sql)

    assert "::uuid[]" not in graph_sql
    assert "::bigint[]" in graph_sql
    assert "memory_edge_sources" in graph_sql
    assert "long_term_memory" in graph_sql
    assert "expires_at IS NULL OR" in graph_sql
    assert "src.user_id = e.user_id" in graph_sql
    assert "tgt.user_id = e.user_id" in graph_sql
    assert "src2.user_id = e2.user_id" in graph_sql
    assert "tgt2.user_id = e2.user_id" in graph_sql
    assert "source.memory_created_at DESC" in graph_sql
    assert "source.memory_id DESC" in graph_sql
    assert "FROM deduplicated" in graph_sql
    assert graph_sql.rindex("ORDER BY effective_weight DESC") < graph_sql.rindex("LIMIT 15")
    assert "::uuid[]" not in temporal_sql
    assert "::bigint[]" in temporal_sql
    assert "memory_edge_sources" in temporal_sql
    assert "src.user_id = e.user_id" in temporal_sql
    assert "tgt.user_id = e.user_id" in temporal_sql


@pytest.mark.asyncio
async def test_source_passage_fetch_is_tenant_and_expiry_bound():
    conn = _FakeConnection()
    query_mock = AsyncMock(
        side_effect=[
            [{"id": 11, "entity_name": "user", "entity_type": "person", "description": "", "sim": 0.9}],
            [
                {
                    "from_name": "user",
                    "predicate": "likes",
                    "to_name": "tea",
                    "weight": 1.0,
                    "is_core": False,
                    "hop": 1,
                    "edge_id": 22,
                    "source_memory_ids": [34, 33],
                    "effective_weight": 1.0,
                }
            ],
            [
                {"id": 33, "content": "Old tea preference"},
                {"id": 34, "content": "Current tea preference"},
            ],
            [],
        ]
    )

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=[])),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
    ):
        _, _, passages = await memory.search_memories_with_graph(42, "short query", "key")

    passage_call = next(
        call for call in query_mock.await_args_list if "SELECT id, content FROM long_term_memory" in call.args[0]
    )
    passage_sql = passage_call.args[0]
    passage_params = passage_call.args[1]

    assert "user_id = $1" in passage_sql
    assert "id = ANY($2::bigint[])" in passage_sql
    assert "expires_at IS NULL OR expires_at > now()" in passage_sql
    assert passage_params == (42, [34, 33])
    assert passages == {"user — likes → tea": "Current tea preference"}


def test_memory_repository_has_no_uuid_array_casts_for_graph_ids():
    source = (ROOT / "app" / "repos" / "memory.py").read_text(encoding="utf-8")

    assert "::uuid[]" not in source


@pytest.mark.asyncio
async def test_store_memory_checks_consent_and_epoch_inside_transaction_then_prunes():
    conn = _FakeConnection()
    calls: list[tuple[str, tuple | None, bool]] = []

    async def fake_query(sql, params=None, *, conn=None):
        calls.append((sql, params, conn.in_transaction))
        if "SELECT ltm_enabled, memory_epoch" in sql:
            return [{"ltm_enabled": True, "memory_epoch": 4}]
        if "INSERT INTO long_term_memory" in sql:
            return [{"id": 99}]
        return []

    async def set_context(*args, **kwargs):
        assert conn.in_transaction

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1, 0.2])),
        patch.object(memory, "set_user_context", AsyncMock(side_effect=set_context)),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        result = await memory.store_memory(42, "A sufficiently long memory", "key", expected_epoch=4)

    assert result == 99
    assert all(in_transaction for _, _, in_transaction in calls)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in conn.execute_calls)

    statements = [sql for sql, _, _ in calls]
    insert_position = next(i for i, sql in enumerate(statements) if "INSERT INTO long_term_memory" in sql)
    prune_position = next(i for i, sql in enumerate(statements) if "DELETE FROM long_term_memory" in sql)
    assert insert_position < prune_position
    prune_call = calls[prune_position]
    assert "OFFSET $2" in prune_call[0]
    assert prune_call[1] == (42, memory.MAX_MEMORIES_PER_USER)
    assert not any("SELECT COUNT(*)" in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_row", "expected_epoch"),
    [
        ({"ltm_enabled": False, "memory_epoch": 4}, 4),
        ({"ltm_enabled": True, "memory_epoch": 5}, 4),
    ],
)
async def test_store_memory_rejects_disabled_or_stale_writes(chat_row, expected_epoch):
    conn = _FakeConnection()
    query_mock = AsyncMock(return_value=[chat_row])

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1, 0.2])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
    ):
        result = await memory.store_memory(42, "A sufficiently long memory", "key", expected_epoch=expected_epoch)

    assert result is None
    assert not any("INSERT INTO long_term_memory" in call.args[0] for call in query_mock.await_args_list)


@pytest.mark.asyncio
async def test_store_memory_treats_missing_chat_as_revoked_before_embedding():
    conn = _FakeConnection()

    @asynccontextmanager
    async def denied_lease(*_args, **_kwargs):
        yield False

    async def fake_query(sql, params=None, *, conn=None):
        if "SELECT ltm_enabled, memory_epoch" in sql:
            return []
        return []

    embedding = AsyncMock(return_value=[0.1, 0.2])
    with (
        patch("app.repos.memory_consent.private_data_lease", denied_lease),
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_get_embedding", embedding),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        result = await memory.store_memory(42, "A sufficiently long memory", "key", expected_epoch=0)

    assert result is None
    embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_memory_propagates_storage_failures():
    conn = _FakeConnection()

    async def fake_query(sql, params=None, *, conn=None):
        if "SELECT ltm_enabled, memory_epoch" in sql:
            return [{"ltm_enabled": True, "memory_epoch": 0}]
        if "INSERT INTO long_term_memory" in sql:
            raise RuntimeError("database unavailable")
        return []

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1, 0.2])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await memory.store_memory(42, "A sufficiently long memory", "key", expected_epoch=0)


@pytest.mark.asyncio
async def test_full_delete_upserts_epoch_before_deleting_memory_and_uses_edge_first_order():
    conn = _FakeConnection()
    manager = SimpleNamespace(pool=_fake_pool(conn), is_connected=True)
    events: list[str] = []

    async def query(sql, *_args, **_kwargs):
        events.append(sql)
        if "memory_consent_epoch_seq" in sql:
            return [{"memory_epoch": 12, "ltm_enabled": True}]
        if "private_data_blocked IS TRUE" in sql:
            return [{"user_id": 42}]
        if "DELETE FROM long_term_memory" in sql:
            return [{"id": 1}]
        return []

    query_mock = AsyncMock(side_effect=query)
    wait_for_leases = AsyncMock()
    conn.transaction = lambda: _AsyncContext(
        None,
        on_enter=lambda: (setattr(conn, "in_transaction", True), events.append("transaction")),
        on_exit=lambda: setattr(conn, "in_transaction", False),
    )

    async def execute(sql: str, *args):
        assert conn.in_transaction
        events.append(sql)
        conn.execute_calls.append((sql, args))
        return "UPDATE 1"

    conn.execute = execute

    async def cancel_tasks(user_id):
        assert user_id == 42
        events.append("cancel")
        return 2

    with (
        patch.object(memory, "db_manager", manager),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", query_mock),
        patch("app.repos.memory_consent.set_user_context", AsyncMock()),
        patch("app.repos.memory_consent.clear_user_context", AsyncMock()),
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", AsyncMock(side_effect=cancel_tasks)),
        patch("app.repos.memory_consent.wait_for_private_data_leases", wait_for_leases),
    ):
        count = await memory.delete_user_memories(42)

    assert count == 1
    assert events[:2] == ["cancel", "transaction"]
    statements = events
    bump_position = next(i for i, sql in enumerate(statements) if "memory_consent_epoch_seq" in sql)
    delete_position = next(i for i, sql in enumerate(statements) if "DELETE FROM long_term_memory" in sql)
    assert bump_position < delete_position
    bump_sql = statements[bump_position]
    assert "nextval('memory_consent_epoch_seq')" in bump_sql
    assert "ON CONFLICT (user_id) DO UPDATE" in bump_sql
    assert "memory_epoch = EXCLUDED.memory_epoch" in bump_sql
    edge_position = next(i for i, sql in enumerate(statements) if "DELETE FROM memory_edges" in sql)
    node_position = next(i for i, sql in enumerate(statements) if "DELETE FROM memory_nodes" in sql)
    assert edge_position < node_position < delete_position

    graph_schema = (ROOT / "scripts" / "migrations" / "024b_add_missing_graph_tables.sql").read_text(encoding="utf-8")
    assert "REFERENCES memory_nodes(id) ON DELETE CASCADE" in graph_schema
    assert "references memory_edges (id, user_id) on delete cascade" in _migration_sql()
    wait_for_leases.assert_awaited_once_with(42, before_epoch=12, ltm_only=True)


@pytest.mark.asyncio
async def test_full_delete_propagates_database_failures_to_user_facing_caller():
    conn = _FakeConnection()
    manager = SimpleNamespace(pool=_fake_pool(conn), is_connected=True)

    async def fake_query(sql, params=None, *, conn=None):
        raise RuntimeError("delete failed")

    with (
        patch.object(memory, "db_manager", manager),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", AsyncMock(side_effect=fake_query)),
        patch("app.repos.memory_consent.set_user_context", AsyncMock()),
        patch("app.repos.memory_consent.clear_user_context", AsyncMock()),
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", AsyncMock(return_value=0)),
    ):
        with pytest.raises(RuntimeError, match="delete failed"):
            await memory.delete_user_memories(42)


@pytest.mark.asyncio
async def test_expiry_cleanup_uses_admin_context_on_same_transaction_connection():
    conn = _FakeConnection()
    query_calls: list[tuple[str, tuple, object, bool]] = []

    async def fake_query(sql, params=None, *, conn=None):
        query_calls.append((sql, params, conn, conn.in_transaction))
        if "DELETE FROM long_term_memory" in sql:
            return [{"id": 1}, {"id": 2}]
        if "SELECT DISTINCT candidate.user_id" in sql:
            return [{"user_id": 42}]
        return [{"delete_stale_orphaned_memory_nodes": 0}]

    async def set_context(user_id, is_admin, *, conn=None):
        assert (user_id, is_admin) == (0, True)
        assert conn.in_transaction

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock(side_effect=set_context)),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        count = await memory.cleanup_expired_memories()

    assert count == 2
    assert len(query_calls) == 4
    assert any("DELETE FROM private_data_leases" in call[0] for call in query_calls)
    sql, params, query_conn, in_transaction = next(
        call for call in query_calls if "DELETE FROM long_term_memory" in call[0]
    )
    assert "expires_at IS NOT NULL" in sql
    assert "expires_at < now()" in sql
    assert params == (42,)
    assert query_conn is conn
    assert in_transaction
    sweep_sql, sweep_params, sweep_conn, sweep_in_transaction = next(
        call for call in query_calls if "delete_stale_orphaned_memory_nodes" in call[0]
    )
    assert "$1" in sweep_sql
    assert sweep_params == (42,)
    assert sweep_conn is conn
    assert sweep_in_transaction
    assert any("pg_advisory_xact_lock" in sql and args == (42,) for sql, args in conn.execute_calls)


@pytest.mark.asyncio
async def test_expiry_cleanup_isolates_users_and_continues_after_one_failure():
    scan_conn = _FakeConnection()
    first_conn = _FakeConnection()
    failing_conn = _FakeConnection()
    last_conn = _FakeConnection()
    pool = _SequencePool([scan_conn, first_conn, failing_conn, last_conn])
    attempted_users: list[int] = []

    async def fake_query(sql, params=None, *, conn=None):
        if "DELETE FROM private_data_leases" in sql:
            assert conn is scan_conn
            return []
        if "SELECT DISTINCT candidate.user_id" in sql:
            assert conn is scan_conn
            return [{"user_id": 1}, {"user_id": 2}, {"user_id": 3}]
        if "DELETE FROM long_term_memory" in sql:
            user_id = params[0]
            attempted_users.append(user_id)
            if user_id == 2:
                raise RuntimeError("user 2 unavailable")
            return [{"id": user_id}]
        if "delete_stale_orphaned_memory_nodes" in sql:
            return [{"delete_stale_orphaned_memory_nodes": 0}]
        raise AssertionError(f"Unexpected SQL: {sql}")

    with (
        patch.object(memory.db_manager, "pool", pool),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        with pytest.raises(RuntimeError, match="incomplete.*1 user"):
            await memory.cleanup_expired_memories()

    assert pool.acquired == [scan_conn, first_conn, failing_conn, last_conn]
    assert attempted_users == [1, 2, 3]
    assert any("pg_advisory_xact_lock" in sql for sql, _ in first_conn.execute_calls)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in last_conn.execute_calls)


@pytest.mark.asyncio
async def test_expiry_cleanup_propagates_candidate_scan_database_failure():
    conn = _FakeConnection()

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=RuntimeError("database unavailable"))),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await memory.cleanup_expired_memories()


@pytest.mark.asyncio
async def test_export_user_memory_is_embedding_free_transactional_and_tenant_bound():
    conn = _FakeConnection()
    calls: list[tuple[str, tuple, bool]] = []

    async def fake_query(sql, params=None, *, conn=None):
        calls.append((sql, params, conn.in_transaction))
        if "FROM long_term_memory" in sql:
            return [{"id": 1, "content": "fact", "source_type": "conversation"}]
        if "FROM memory_nodes" in sql:
            return [{"id": 2, "entity_name": "user"}]
        if "FROM memory_edges" in sql:
            return [{"id": 3, "predicate": "likes"}]
        if "FROM memory_edge_sources" in sql:
            return [{"edge_id": 3, "memory_id": 1, "user_id": 42}]
        if "FROM memory_node_sources" in sql:
            return [{"node_id": 2, "memory_id": 1, "user_id": 42}]
        if "FROM memory_derivation_sources" in sql:
            return [{"derived_memory_id": 4, "source_memory_id": 1, "user_id": 42}]
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def set_context(*args, **kwargs):
        assert conn.in_transaction

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock(side_effect=set_context)),
        patch.object(memory, "db_query", AsyncMock(side_effect=fake_query)),
    ):
        exported = await memory.export_user_memory(42)

    assert exported == {
        "memories": [{"id": 1, "content": "fact", "source_type": "conversation"}],
        "nodes": [{"id": 2, "entity_name": "user"}],
        "edges": [{"id": 3, "predicate": "likes"}],
        "edge_sources": [{"edge_id": 3, "memory_id": 1, "user_id": 42}],
        "node_sources": [{"node_id": 2, "memory_id": 1, "user_id": 42}],
        "derivation_sources": [{"derived_memory_id": 4, "source_memory_id": 1, "user_id": 42}],
    }
    assert all(in_transaction for _, _, in_transaction in calls)
    assert all(params == (42,) for _, params, _ in calls)
    assert all("WHERE user_id = $1" in sql for sql, _, _ in calls)
    assert all("embedding" not in sql.lower() for sql, _, _ in calls)

    memory_sql = next(sql for sql, _, _ in calls if "FROM long_term_memory" in sql)
    for column in (
        "metadata",
        "created_at",
        "expires_at",
        "consolidated_at",
        "wing",
        "room",
        "hall_type",
    ):
        assert column in memory_sql


@pytest.mark.asyncio
async def test_export_user_memory_propagates_database_failures():
    conn = _FakeConnection()

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(side_effect=RuntimeError("export failed"))),
    ):
        with pytest.raises(RuntimeError, match="export failed"):
            await memory.export_user_memory(42)


@pytest.mark.asyncio
async def test_response_edge_attribution_is_exact_and_empty_retrieval_clears_context():
    conn = _FakeConnection()
    successful_queries = AsyncMock(
        side_effect=[
            [{"id": 11, "entity_name": "user", "entity_type": "person", "description": "", "sim": 0.9}],
            [
                {
                    "from_name": "user",
                    "predicate": "likes",
                    "to_name": "tea",
                    "weight": 1.0,
                    "is_core": False,
                    "hop": 1,
                    "edge_id": 22,
                    "source_memory_ids": [],
                    "effective_weight": 1.0,
                }
            ],
            [],
        ]
    )

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=[])),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", successful_queries),
    ):
        await memory.search_memories_with_graph(42, "short query", "key")

    assert memory.bind_retrieved_edges_to_response(42, 1001) == [22]
    assert memory.get_response_retrieved_edge_ids(42, 1001) == [22]
    assert memory.get_response_retrieved_edge_ids(42, 9999) == []
    assert memory.get_response_retrieved_edge_ids(7, 1001) == []

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
        patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
        patch.object(memory, "search_memories", AsyncMock(return_value=[])),
        patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", AsyncMock(return_value=[])),
    ):
        await memory.search_memories_with_graph(42, "short query", "key")

    assert memory.bind_retrieved_edges_to_response(42, 1002) == []
    assert memory.get_response_retrieved_edge_ids(42, 1002) == []
    assert memory.get_response_retrieved_edge_ids(42, 1001) == [22]


def test_response_edge_attribution_cache_is_bounded_and_expiring():
    from cachetools import TTLCache

    cache = memory._response_retrieved_edge_ids

    assert isinstance(cache, TTLCache)
    assert cache.maxsize == 4096
    assert cache.ttl == 3600


@pytest.mark.asyncio
async def test_penalize_without_explicit_attribution_is_strict_noop():
    class _PenaltyConnection:
        async def execute(self, *args):
            return "UPDATE 1"

    conn = _PenaltyConnection()
    query_mock = AsyncMock(return_value=[{"id": 77}])
    legacy = getattr(memory, "_last_retrieved_edge_ids", None)
    if legacy is not None:
        legacy[42] = [77]
    memory._current_retrieved_edge_ids.set((42, (77,)))
    memory._response_retrieved_edge_ids[(42, 9001)] = (77,)

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(memory, "set_user_context", AsyncMock()),
        patch.object(memory, "db_query", query_mock),
    ):
        count = await memory.penalize_graph_edges(42, None)

    assert count == 0
    query_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_edge_penalty_runs_with_transaction_local_context():
    conn = _FakeConnection()

    with (
        patch.object(memory.db_manager, "pool", _fake_pool(conn)),
        patch.object(
            memory,
            "set_user_context",
            AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
        ),
        patch.object(memory, "db_query", AsyncMock(return_value=[])),
    ):
        count = await memory.penalize_graph_edges(42, [77])

    assert count == 1


def test_memory_repository_does_not_clear_transaction_local_rls_context_manually():
    source = (ROOT / "app" / "repos" / "memory.py").read_text(encoding="utf-8")

    assert "await clear_user_context" not in source


@pytest.mark.asyncio
async def test_edge_attribution_can_cross_child_task_via_explicit_ids():
    conn = _FakeConnection()

    async def retrieve_in_child_task():
        queries = AsyncMock(
            side_effect=[
                [{"id": 11, "entity_name": "user", "entity_type": "person", "description": "", "sim": 0.9}],
                [
                    {
                        "from_name": "user",
                        "predicate": "likes",
                        "to_name": "tea",
                        "weight": 1.0,
                        "is_core": False,
                        "hop": 1,
                        "edge_id": 22,
                        "source_memory_ids": [],
                        "effective_weight": 1.0,
                    }
                ],
                [],
            ]
        )
        with (
            patch.object(memory.db_manager, "pool", _fake_pool(conn)),
            patch.object(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True)),
            patch.object(memory, "_lock_ltm_read_consent", AsyncMock(return_value=True)),
            patch.object(memory, "search_memories", AsyncMock(return_value=[])),
            patch.object(memory, "_get_embedding", AsyncMock(return_value=[0.1])),
            patch.object(memory, "set_user_context", AsyncMock()),
            patch.object(memory, "db_query", queries),
        ):
            await memory.search_memories_with_graph(42, "short query", "key")
        return memory.get_current_retrieved_edge_ids(42)

    import asyncio

    child_edge_ids = await asyncio.create_task(retrieve_in_child_task())

    assert child_edge_ids == [22]
    assert memory.bind_retrieved_edges_to_response(42, 2001, edge_ids=child_edge_ids) == [22]
    assert memory.get_response_retrieved_edge_ids(42, 2001) == [22]

"""Safety regression tests for long-term-memory consolidation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.repos import memory as memory_repo
from app.repos import memory_consolidation as consolidation


@pytest.fixture(autouse=True)
def _allow_private_data_lease_boundary():
    @asynccontextmanager
    async def allowed(*_args, **_kwargs):
        yield True

    with patch("app.repos.memory_consent.private_data_lease", allowed):
        yield


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingPool:
    def __init__(self, conn):
        self.conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _AsyncContext(self.conn)


class _RecordingTransaction:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_transaction = True
        self.conn.calls.append(("begin", "", ()))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.calls.append(("rollback" if exc_type else "commit", "", ()))
        self.conn.in_transaction = False
        return False


class _ConsolidationConnection:
    def __init__(
        self,
        *,
        snapshot_rows,
        inserted_fact_ids=(),
        similar_edge_id: int | None = None,
        stale_merge: bool = False,
        preflight_rows=None,
    ):
        self.snapshot_rows = snapshot_rows
        self.preflight_rows = snapshot_rows if preflight_rows is None else preflight_rows
        self.inserted_fact_ids = list(inserted_fact_ids)
        self.fact_insert_index = 0
        self.similar_edge_id = similar_edge_id
        self.stale_merge = stale_merge
        self.calls: list[tuple[str, str, tuple]] = []
        self.in_transaction = False

    def transaction(self):
        return _RecordingTransaction(self)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def executemany(self, sql, args):
        rows = tuple(args)
        self.calls.append(("executemany", sql, rows))
        return None

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        if "ltm_enabled" in sql:
            return True
        return None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        normalized = " ".join(sql.split())
        if "FROM long_term_memory" in normalized and "FOR UPDATE" in normalized:
            return self.snapshot_rows
        if "FROM long_term_memory" in normalized:
            return self.preflight_rows
        if "UPDATE long_term_memory" in normalized and "consolidated_at" in normalized:
            return [{"id": row["id"]} for row in self.snapshot_rows]
        if "INSERT INTO memory_nodes" in normalized:
            names = args[1]
            return [{"id": index + 101, "entity_name": name} for index, name in enumerate(names)]
        if "FROM unnest" in normalized and "memory_nodes" in normalized:
            return []
        if "FROM unnest" in normalized and "memory_edges" in normalized:
            if self.similar_edge_id is not None:
                return [{"idx": 0, "id": self.similar_edge_id, "predicate": "uses"}]
            return []
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if "INSERT INTO long_term_memory" in sql:
            if self.fact_insert_index >= len(self.inserted_fact_ids):
                return None
            fact_id = self.inserted_fact_ids[self.fact_insert_index]
            self.fact_insert_index += 1
            return {"id": fact_id}
        if "UPDATE memory_edges" in sql:
            if self.stale_merge:
                return None
            return {"id": self.similar_edge_id}
        if "INSERT INTO memory_edges" in sql:
            return {"id": 502 if self.stale_merge else 501}
        return None


def _raw_memory(memory_id: int, *, expires_at=None, epoch: int = 7):
    return {
        "id": memory_id,
        "content": f"raw memory {memory_id}",
        "source_type": "conversation",
        "created_at": datetime(2026, 8, memory_id, tzinfo=UTC),
        "expires_at": expires_at,
        "memory_epoch": epoch,
        "est_tokens": 4,
    }


def _fact(text: str, *source_ids: int):
    return {"text": text, "source_ids": list(source_ids)}


def _relation(*, support_fact_indexes, predicate: str = "uses"):
    return {
        "from": "Alice",
        "to": "Python",
        "predicate": predicate,
        "weight": 0.8,
        "is_core": False,
        "support_fact_indexes": list(support_fact_indexes),
    }


def _install_db(monkeypatch, conn):
    pool = _RecordingPool(conn)
    monkeypatch.setattr(consolidation, "db_manager", SimpleNamespace(pool=pool))
    monkeypatch.setattr(consolidation, "set_user_context", AsyncMock())
    return pool


@pytest.mark.asyncio
async def test_raw_snapshot_excludes_already_consolidated_and_disabled_memory(monkeypatch):
    seen_sql: list[str] = []
    expiry = datetime.now(UTC) + timedelta(days=2)
    conn = _ConsolidationConnection(snapshot_rows=[])

    async def fake_db_query(sql, params=None, *, conn=None):
        assert conn.in_transaction
        seen_sql.append(sql)
        return [
            {
                "id": 1,
                "content": "raw memory 1",
                "source_type": "conversation",
                "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                "expires_at": expiry,
                "memory_epoch": 11,
            }
        ]

    _install_db(monkeypatch, conn)
    monkeypatch.setattr(
        consolidation,
        "set_user_context",
        AsyncMock(side_effect=lambda *args, **kwargs: assert_in_transaction(conn)),
    )
    monkeypatch.setattr(consolidation, "db_query", fake_db_query)

    rows = await consolidation.get_raw_memories_for_consolidation(42)

    sql = " ".join(seen_sql[0].split())
    assert "consolidated_at IS NULL" in sql
    assert "ltm_enabled IS TRUE" in sql
    assert rows[0]["expires_at"] == expiry
    assert rows[0]["memory_epoch"] == 11
    assert [kind for kind, _, _ in conn.calls if kind in {"begin", "commit"}] == ["begin", "commit"]


def assert_in_transaction(conn):
    assert conn.in_transaction


@pytest.mark.asyncio
async def test_last_consolidation_read_uses_transaction_scoped_rls(monkeypatch):
    conn = _ConsolidationConnection(snapshot_rows=[])
    _install_db(monkeypatch, conn)
    last_ts = datetime(2026, 8, 20, tzinfo=UTC)

    async def fake_db_query(sql, params=None, *, conn=None):
        assert conn.in_transaction
        return [{"last_ts": last_ts}]

    async def set_context(*args, **kwargs):
        assert conn.in_transaction

    monkeypatch.setattr(consolidation, "set_user_context", set_context)
    monkeypatch.setattr(consolidation, "db_query", fake_db_query)

    assert await consolidation.get_last_consolidation_time(42) == last_ts
    assert [kind for kind, _, _ in conn.calls if kind in {"begin", "commit"}] == ["begin", "commit"]


@pytest.mark.asyncio
async def test_embedding_failure_aborts_before_database_mutation(monkeypatch):
    raw = [_raw_memory(1), _raw_memory(2)]
    conn = _ConsolidationConnection(snapshot_rows=raw)
    pool = _install_db(monkeypatch, conn)
    monkeypatch.setattr(
        consolidation,
        "_extract_graph",
        AsyncMock(return_value={"facts": [_fact("fact one", 1), _fact("fact two", 2)], "entities": [], "relations": []}),
    )
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(side_effect=[[0.1, 0.2], None]))

    result = await consolidation.consolidate_memories(42, "key", _prefetched_memories=raw)

    assert result == 0
    assert pool.acquire_count == 1
    assert not any("INSERT INTO" in sql for _, sql, _ in conn.calls)


@pytest.mark.asyncio
async def test_stale_snapshot_is_rejected_under_user_lock(monkeypatch):
    raw = [_raw_memory(1), _raw_memory(2)]
    conn = _ConsolidationConnection(snapshot_rows=[raw[0]], inserted_fact_ids=[9001], preflight_rows=raw)
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(
        consolidation,
        "_extract_graph",
        AsyncMock(return_value={"facts": [_fact("fact one", 1)], "entities": [], "relations": []}),
    )
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 0
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert any("pg_advisory_xact_lock" in sql for sql in statements)
    assert not any("INSERT INTO long_term_memory" in sql for sql in statements)
    assert not any("SET consolidated_at" in sql for sql in statements)


@pytest.mark.asyncio
async def test_consolidation_marks_sources_after_insert_and_returns_database_count(monkeypatch):
    soon = datetime.now(UTC) + timedelta(days=1)
    later = datetime.now(UTC) + timedelta(days=5)
    raw = [_raw_memory(1, expires_at=later), _raw_memory(2, expires_at=soon)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001])
    _install_db(monkeypatch, conn)

    async def set_context_inside_transaction(*args, **kwargs):
        assert conn.in_transaction

    monkeypatch.setattr(consolidation, "set_user_context", set_context_inside_transaction)
    monkeypatch.setattr(
        consolidation,
        "_extract_graph",
        AsyncMock(return_value={"facts": [_fact("fact one", 1, 2)], "entities": [], "relations": []}),
    )
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 1
    statements = [(kind, " ".join(sql.split()), args) for kind, sql, args in conn.calls if sql]
    assert not any("DELETE FROM long_term_memory" in sql for _, sql, _ in statements)
    snapshot_sql = next(sql for _, sql, _ in statements if "FROM long_term_memory" in sql and "FOR UPDATE" in sql)
    assert "FOR SHARE OF c" in snapshot_sql
    lock_index = next(i for i, (_, sql, _) in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    insert_index = next(i for i, (_, sql, _) in enumerate(statements) if "INSERT INTO long_term_memory" in sql)
    mark_index = next(i for i, (_, sql, _) in enumerate(statements) if "SET consolidated_at" in sql)
    assert lock_index < insert_index < mark_index
    _, lock_sql, lock_args = statements[lock_index]
    assert "hashtextextended" not in lock_sql
    assert lock_args == (42,)

    _, fact_insert_sql, fact_insert_args = statements[insert_index]
    assert "expires_at" in fact_insert_sql
    assert soon in fact_insert_args


@pytest.mark.asyncio
async def test_consolidated_edges_reference_inserted_fact_ids(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001, 9002])
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1), _fact("fact two", 1)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
        ],
        "relations": [_relation(support_fact_indexes=[0, 1])],
    }
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 2
    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    assert all("uuid[]" not in sql for sql, _ in statements)
    edge_sql = next(sql for sql, _ in statements if "INSERT INTO memory_edges" in sql)
    assert "ON CONFLICT (user_id, source_node, target_node, predicate) WHERE valid_to IS NULL" in edge_sql
    assert "source_memory_ids" in edge_sql
    provenance_sql, provenance_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql
    )
    assert "ON CONFLICT (edge_id, memory_id) DO UPDATE" in provenance_sql
    assert "attributes_complete = TRUE" in provenance_sql
    flattened_args = repr(provenance_args)
    assert "9001" in flattened_args and "9002" in flattened_args and "501" in flattened_args


@pytest.mark.asyncio
async def test_semantically_similar_distinct_predicate_is_not_merged(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(
        snapshot_rows=raw,
        inserted_fact_ids=[9001],
        similar_edge_id=501,
    )
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
        ],
        "relations": [_relation(support_fact_indexes=[0], predicate="works with")],
    }
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    assert (
        await consolidation.consolidate_memories(
            42,
            "key",
            _prefetched_memories=raw,
            expected_epoch=7,
        )
        == 1
    )

    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert not any("predicate_embedding <=>" in sql and "FROM memory_edges" in sql for sql in statements)
    assert any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_consolidation_writes_source_level_graph_attributes(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001])
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
        ],
        "relations": [_relation(support_fact_indexes=[0])],
    }
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    assert (
        await consolidation.consolidate_memories(
            42,
            "key",
            _prefetched_memories=raw,
            expected_epoch=7,
        )
        == 1
    )

    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    node_source_sql, node_source_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_node_sources" in sql
    )
    assert "attributes_complete" in node_source_sql
    assert "9001" in repr(node_source_args)
    # Entity descriptions have no entity-level support indexes in the model
    # schema, so consolidation must not attribute them to relation facts.
    assert "user" not in repr(node_source_args)
    assert "language" not in repr(node_source_args)
    embedded_inputs = [call.args[0] for call in embeddings.await_args_list]
    assert "Alice: user" not in embedded_inputs
    assert "Python: language" not in embedded_inputs
    edge_source_sql, edge_source_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql
    )
    for attribute in ("predicate", "predicate_embedding", "weight", "is_core", "attributes_complete"):
        assert attribute in edge_source_sql
    assert "9001" in repr(edge_source_args)


@pytest.mark.asyncio
async def test_exact_edge_upsert_does_not_depend_on_stale_semantic_candidate(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(
        snapshot_rows=raw,
        inserted_fact_ids=[9001],
        similar_edge_id=501,
        stale_merge=True,
    )
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
        ],
        "relations": [_relation(support_fact_indexes=[0])],
    }
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 1
    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    fallback_sql = next(sql for sql, _ in statements if "INSERT INTO memory_edges" in sql)
    assert "ON CONFLICT (user_id, source_node, target_node, predicate) WHERE valid_to IS NULL" in fallback_sql
    assert "DO UPDATE SET updated_at = now()" in fallback_sql
    assert not any("predicate_embedding <=>" in sql and "FROM memory_edges" in sql for sql, _ in statements)
    _, provenance_args = next((sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql)
    assert "502" in repr(provenance_args)
    assert "501" not in repr(provenance_args)


@pytest.mark.asyncio
async def test_consolidation_persists_only_entities_used_by_relations(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001])
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
            {"name": "Private note", "type": "concept", "description": "unused PII"},
        ],
        "relations": [_relation(support_fact_indexes=[0])],
    }
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 1
    assert embeddings.await_count == 4  # fact + two relation endpoints + predicate
    node_insert_args = next(args for _, sql, args in conn.calls if "INSERT INTO memory_nodes" in " ".join(sql.split()))
    assert set(node_insert_args[1]) == {"Alice", "Python"}
    assert "Private note" not in repr(node_insert_args)


@pytest.mark.asyncio
async def test_consolidation_ignores_relations_with_missing_endpoint_entities(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001])
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1)],
        "entities": [{"name": "Alice", "type": "person", "description": "user"}],
        "relations": [
            {
                "from": "Alice",
                "to": "Missing",
                "predicate": "knows",
                "weight": 0.8,
                "support_fact_indexes": [0],
            }
        ],
    }
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 1
    assert embeddings.await_count == 1  # the consolidated fact only
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert not any("INSERT INTO memory_nodes" in sql for sql in statements)
    assert not any("INSERT INTO memory_edges" in sql for sql in statements)
    assert not any("INSERT INTO memory_edge_sources" in sql for sql in statements)


@pytest.mark.asyncio
async def test_consolidation_labels_raw_ids_in_external_extraction_prompt(monkeypatch):
    raw = [_raw_memory(1), _raw_memory(2)]
    conn = _ConsolidationConnection(snapshot_rows=raw)
    _install_db(monkeypatch, conn)
    seen_text: list[str] = []

    async def extract(memories_text, api_key):
        seen_text.append(memories_text)
        return {"facts": [], "entities": [], "relations": []}

    monkeypatch.setattr(consolidation, "_extract_graph", extract)

    assert await consolidation.consolidate_memories(42, "key", _prefetched_memories=raw) == 0
    assert "[memory_id=1 " in seen_text[0]
    assert "[memory_id=2 " in seen_text[0]


@pytest.mark.asyncio
async def test_consolidation_rechecks_snapshot_after_extraction_before_embeddings(monkeypatch):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=[])
    pool = _install_db(monkeypatch, conn)
    graph = {"facts": [_fact("fact one", 1)], "entities": [], "relations": []}
    extract = AsyncMock(return_value=graph)
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(consolidation, "_extract_graph", extract)
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 0
    extract.assert_awaited_once()
    embeddings.assert_not_awaited()
    assert pool.acquire_count == 1
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert any("FROM long_term_memory" in sql and "FOR UPDATE" not in sql for sql in statements)
    assert not any("INSERT INTO long_term_memory" in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph",
    [
        {"facts": [{"text": "fact one"}], "entities": [], "relations": []},
        {"facts": [_fact("fact one", 999)], "entities": [], "relations": []},
        {
            "facts": [_fact("fact one", 1)],
            "entities": [
                {"name": "Alice", "type": "person", "description": "user"},
                {"name": "Python", "type": "skill", "description": "language"},
            ],
            "relations": [_relation(support_fact_indexes=[])],
        },
        {
            "facts": [_fact("fact one", 1)],
            "entities": [
                {"name": "Alice", "type": "person", "description": "user"},
                {"name": "Python", "type": "skill", "description": "language"},
            ],
            "relations": [_relation(support_fact_indexes=[1])],
        },
    ],
)
async def test_invalid_fact_or_relation_provenance_aborts_before_embeddings(monkeypatch, graph):
    raw = [_raw_memory(1)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001])
    _install_db(monkeypatch, conn)
    embeddings = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", embeddings)

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 0
    embeddings.assert_not_awaited()
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert not any("INSERT INTO long_term_memory" in sql for sql in statements)
    assert not any("SET consolidated_at" in sql for sql in statements)


@pytest.mark.asyncio
async def test_consolidation_persists_exact_derivation_and_edge_provenance(monkeypatch):
    raw = [_raw_memory(1), _raw_memory(2)]
    conn = _ConsolidationConnection(snapshot_rows=raw, inserted_fact_ids=[9001, 9002])
    _install_db(monkeypatch, conn)
    graph = {
        "facts": [_fact("fact one", 1), _fact("fact two", 2)],
        "entities": [
            {"name": "Alice", "type": "person", "description": "user"},
            {"name": "Python", "type": "skill", "description": "language"},
        ],
        "relations": [_relation(support_fact_indexes=[1])],
    }
    monkeypatch.setattr(consolidation, "_extract_graph", AsyncMock(return_value=graph))
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    result = await consolidation.consolidate_memories(
        42,
        "key",
        _prefetched_memories=raw,
        expected_epoch=7,
    )

    assert result == 2
    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    derivation_sql, derivation_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_derivation_sources" in sql
    )
    assert "ON CONFLICT (derived_memory_id, source_memory_id) DO NOTHING" in derivation_sql
    assert derivation_args[:2] == ([9001, 9002], [1, 2])

    edge_sql = next(sql for sql, _ in statements if "INSERT INTO memory_edges" in sql)
    assert "DO UPDATE SET updated_at = now()" in edge_sql
    assert "GREATEST" not in edge_sql
    _, edge_args = next((sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql)
    assert edge_args[1] == [9002]
    assert 9001 not in edge_args[1]

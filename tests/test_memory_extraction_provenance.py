"""Consent and provenance regressions for real-time graph extraction."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import database
from app.repos import db_helpers
from app.repos import memory as memory_repo
from app.repos import memory_extraction as extraction


@pytest.fixture(autouse=True)
def _allow_private_data_lease_boundary():
    @asynccontextmanager
    async def allowed(*_args, **_kwargs):
        yield True

    with patch("app.repos.memory_consent.private_data_lease", allowed):
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

    async def __aexit__(self, exc_type, exc, tb):
        if self.on_exit:
            self.on_exit()
        return False


class _Transaction:
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


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _AsyncContext(
            self.conn,
            on_enter=lambda: setattr(self.conn, "acquire_depth", self.conn.acquire_depth + 1),
            on_exit=lambda: setattr(self.conn, "acquire_depth", self.conn.acquire_depth - 1),
        )


class _ExtractionConnection:
    def __init__(self, *, mode: str = "new", consent: bool | list[bool] = True):
        self.mode = mode
        self.consent = list(consent) if isinstance(consent, list) else consent
        self.calls: list[tuple[str, str, tuple]] = []
        self.node_index = 0
        self.node_lookup_index = 0
        self.in_transaction = False
        self.acquire_depth = 0
        self.fetchval_transaction_states: list[bool] = []

    def transaction(self):
        return _Transaction(self)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        self.fetchval_transaction_states.append(self.in_transaction)
        if "ltm_enabled" in sql:
            if isinstance(self.consent, list):
                return self.consent.pop(0)
            return self.consent
        return None

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        normalized = " ".join(sql.split())
        if "SELECT id, entity_name" in normalized:
            if self.mode in {"merge", "refinement", "far_parallel"}:
                names = ("Alice", "Python")
                index = self.node_lookup_index % len(names)
                self.node_lookup_index += 1
                return {"id": 101 + index, "entity_name": names[index]}
            return None
        if "INSERT INTO memory_nodes" in normalized:
            self.node_index += 1
            return {"id": 100 + self.node_index}
        if "SELECT id, predicate, weight" in normalized:
            if self.mode == "merge":
                return {"id": 501, "predicate": "uses", "weight": 0.4}
            return None
        if "UPDATE memory_edges" in normalized and "RETURNING id" in normalized:
            return {"id": 501 if self.mode == "merge" else 502}
        if "INSERT INTO memory_edges" in normalized:
            return {"id": 503}
        return None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        normalized = " ".join(sql.split())
        if "AS distance" in normalized and self.mode == "refinement":
            return [{"id": 502, "predicate": "works with", "distance": 0.2}]
        if "AS distance" in normalized and self.mode == "far_parallel":
            return [{"id": 504, "predicate": "mentors", "distance": 0.8}]
        return []

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def executemany(self, sql, args):
        rows = tuple(args)
        self.calls.append(("executemany", sql, rows))
        return None


def _graph():
    return extraction.GraphExtractionResult(
        entities=[
            extraction.ExtractedEntity(name="Alice", type="person", description="user"),
            extraction.ExtractedEntity(name="Python", type="skill", description="language"),
        ],
        relations=[extraction.ExtractedRelation(source="Alice", target="Python", predicate="uses", weight=0.8)],
    )


def _install_db(monkeypatch, conn):
    pool = _Pool(conn)
    monkeypatch.setattr(database, "db_manager", SimpleNamespace(pool=pool))
    monkeypatch.setattr(db_helpers, "set_user_context", AsyncMock())
    monkeypatch.setattr(db_helpers, "clear_user_context", AsyncMock())
    return pool


@pytest.mark.asyncio
async def test_extract_entrypoint_forwards_expected_epoch(monkeypatch):
    conn = _ExtractionConnection(consent=True)
    _install_db(monkeypatch, conn)
    graph = _graph()
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(extraction, "extract_graph_structured", AsyncMock(return_value=graph))
    monkeypatch.setattr(extraction, "_upsert_graph", upsert)

    result = await extraction.extract_and_store_graph(
        42,
        "This user message is long enough to extract a useful graph fact.",
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    assert upsert.await_args.kwargs["expected_epoch"] == 9


@pytest.mark.asyncio
async def test_extract_entrypoint_rejects_stale_source_before_external_extraction(monkeypatch):
    conn = _ExtractionConnection(consent=False)
    pool = _install_db(monkeypatch, conn)
    extract = AsyncMock(return_value=_graph())
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(extraction, "extract_graph_structured", extract)
    monkeypatch.setattr(extraction, "_upsert_graph", upsert)

    result = await extraction.extract_and_store_graph(
        42,
        "This user message is long enough to extract a useful graph fact.",
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 0
    extract.assert_not_awaited()
    upsert.assert_not_awaited()
    assert pool.acquire_count == 1
    preflight_call = next(
        (sql, args) for kind, sql, args in conn.calls if kind == "fetchval" and "long_term_memory" in sql
    )
    preflight_sql, preflight_args = preflight_call
    normalized_preflight = " ".join(preflight_sql.split())
    assert "memory.id = $2" in normalized_preflight
    assert "memory.user_id = $1" in normalized_preflight
    assert "chat.ltm_enabled IS TRUE" in normalized_preflight
    assert "chat.memory_epoch = $3" in normalized_preflight
    assert "memory.expires_at" in normalized_preflight
    assert preflight_args == (42, 77, 9)
    assert conn.fetchval_transaction_states == [True]
    db_helpers.set_user_context.assert_awaited_once_with(42, False, conn=conn)
    assert not any(kind == "execute" for kind, _, _ in conn.calls)


@pytest.mark.asyncio
async def test_extract_entrypoint_rechecks_source_before_embeddings(monkeypatch):
    conn = _ExtractionConnection(consent=[True, False])
    pool = _install_db(monkeypatch, conn)

    async def extract_after_preflight(*args, **kwargs):
        assert conn.in_transaction is False
        assert conn.acquire_depth == 0
        return _graph()

    extract = AsyncMock(side_effect=extract_after_preflight)
    embedding = AsyncMock(return_value=[0.1, 0.2])
    resolver = AsyncMock(return_value="parallel")
    monkeypatch.setattr(extraction, "extract_graph_structured", extract)
    monkeypatch.setattr(memory_repo, "_get_embedding", embedding)
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolver)

    result = await extraction.extract_and_store_graph(
        42,
        "This user message is long enough to extract a useful graph fact.",
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 0
    extract.assert_awaited_once()
    embedding.assert_not_awaited()
    resolver.assert_not_awaited()
    assert pool.acquire_count == 2


@pytest.mark.asyncio
async def test_extract_entrypoint_without_source_memory_fails_closed(monkeypatch):
    extract = AsyncMock(return_value=_graph())
    upsert = AsyncMock(return_value=1)
    monkeypatch.setattr(extraction, "extract_graph_structured", extract)
    monkeypatch.setattr(extraction, "_upsert_graph", upsert)

    result = await extraction.extract_and_store_graph(
        42,
        "This user message is long enough to extract a useful graph fact.",
        "key",
    )

    assert result == 0
    extract.assert_not_awaited()
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_graph_upsert_without_source_memory_fails_before_external_work(monkeypatch):
    conn = _ExtractionConnection()
    pool = _install_db(monkeypatch, conn)
    embedding = AsyncMock(return_value=[0.1, 0.2])
    monkeypatch.setattr(memory_repo, "_get_embedding", embedding)

    result = await extraction._upsert_graph(42, _graph(), "key")

    assert result == 0
    embedding.assert_not_awaited()
    assert pool.acquire_count == 0
    assert conn.calls == []


@pytest.mark.asyncio
async def test_stale_or_disabled_extraction_does_not_mutate_graph(monkeypatch):
    conn = _ExtractionConnection(consent=False)
    _install_db(monkeypatch, conn)
    embedding = AsyncMock(return_value=[0.1, 0.2])
    resolver = AsyncMock(return_value="parallel")
    monkeypatch.setattr(memory_repo, "_get_embedding", embedding)
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolver)

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 0
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    consent_sql = next(sql for sql in statements if "ltm_enabled" in sql)
    assert "memory_epoch" in consent_sql
    assert "long_term_memory" in consent_sql
    embedding.assert_not_awaited()
    resolver.assert_not_awaited()
    assert not any("INSERT INTO memory_nodes" in sql for sql in statements)
    assert not any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_edge_id"),
    [("merge", 503), ("refinement", 503), ("new", 503)],
)
async def test_every_edge_path_writes_exact_source_attribute_provenance(
    monkeypatch,
    mode,
    expected_edge_id,
):
    conn = _ExtractionConnection(mode=mode)
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", AsyncMock(return_value="refinement"))

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    assert all("uuid[]" not in sql for sql, _ in statements)
    consent_sql = [sql for sql, _ in statements if "ltm_enabled" in sql]
    assert any("FROM long_term_memory AS memory" in sql and "FOR SHARE" not in sql for sql in consent_sql)
    assert all("FOR SHARE OF c" in sql for sql in consent_sql if "FOR KEY SHARE OF m" in sql)

    edge_sql = next(sql for sql, _ in statements if "INSERT INTO memory_edges" in sql)
    assert "ON CONFLICT (user_id, source_node, target_node, predicate) WHERE valid_to IS NULL" in edge_sql
    assert "DO UPDATE SET updated_at = now()" in edge_sql
    assert "GREATEST" not in edge_sql

    provenance_sql, provenance_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql
    )
    assert "ON CONFLICT (edge_id, memory_id) DO UPDATE" in provenance_sql
    assert "attributes_complete = TRUE" in provenance_sql
    flattened_args = repr(provenance_args)
    assert str(expected_edge_id) in flattened_args
    assert "77" in flattened_args
    assert "42" in flattened_args


@pytest.mark.asyncio
async def test_ambiguous_llm_resolution_runs_outside_transaction_and_rechecks_consent(monkeypatch):
    conn = _ExtractionConnection(mode="refinement", consent=[True, True, True, False])
    pool = _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    resolver_transaction_states: list[bool] = []
    resolver_acquire_depths: list[int] = []

    async def resolve(*args, **kwargs):
        resolver_transaction_states.append(conn.in_transaction)
        resolver_acquire_depths.append(conn.acquire_depth)
        return "refinement"

    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolve)

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 0
    assert resolver_transaction_states == [False]
    assert resolver_acquire_depths == [0]
    assert pool.acquire_count == 4
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert sum("ltm_enabled" in sql for sql in statements) == 4
    assert not any("INSERT INTO memory_nodes" in sql for sql in statements)
    assert not any("UPDATE memory_edges" in sql for sql in statements)
    assert not any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_revocation_after_conflict_read_stops_before_external_resolver(monkeypatch):
    conn = _ExtractionConnection(mode="refinement", consent=[True, True, False])
    pool = _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    resolver = AsyncMock(return_value="parallel")
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolver)

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 0
    resolver.assert_not_awaited()
    assert pool.acquire_count == 3
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert sum("ltm_enabled" in sql for sql in statements) == 3
    assert not any("INSERT INTO memory_nodes" in sql for sql in statements)
    assert not any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_nodes_are_upserted_only_for_relation_endpoints_after_final_recheck(monkeypatch):
    conn = _ExtractionConnection(mode="new", consent=[True, True, True])
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    graph = extraction.GraphExtractionResult(
        entities=[
            extraction.ExtractedEntity(name="Alice", type="person", description="user"),
            extraction.ExtractedEntity(name="Python", type="skill", description="language"),
            extraction.ExtractedEntity(name="Private note", type="concept", description="unused PII"),
        ],
        relations=[extraction.ExtractedRelation(source="Alice", target="Python", predicate="uses", weight=0.8)],
    )

    result = await extraction._upsert_graph(
        42,
        graph,
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    sql_calls = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    consent_positions = [index for index, (sql, _) in enumerate(sql_calls) if "ltm_enabled" in sql]
    node_writes = [(index, args) for index, (sql, args) in enumerate(sql_calls) if "INSERT INTO memory_nodes" in sql]
    assert len(consent_positions) == 3
    assert node_writes
    assert all(index > consent_positions[-1] for index, _ in node_writes)
    assert all("Private note" not in repr(args) for _, args in node_writes)
    assert any("pg_advisory_xact_lock($1)" in sql for sql, _ in sql_calls)
    assert any("FOR KEY SHARE" in sql and "long_term_memory" in sql for sql, _ in sql_calls)


@pytest.mark.asyncio
async def test_far_predicate_stays_parallel_without_explicit_update_verdict(monkeypatch):
    conn = _ExtractionConnection(mode="far_parallel")
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    resolver = AsyncMock(return_value="parallel")
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolver)

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    resolver.assert_awaited_once()
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert not any("SET valid_to = now()" in sql for sql in statements)
    assert any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_edge_is_superseded_only_after_explicit_update_verdict(monkeypatch):
    conn = _ExtractionConnection(mode="far_parallel")
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    resolver = AsyncMock(return_value="update")
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", resolver)

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    resolver.assert_awaited_once()
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert sum("SET valid_to = now()" in sql for sql in statements) == 1
    assert any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_refinement_keeps_exact_predicates_as_parallel_edges(monkeypatch):
    conn = _ExtractionConnection(mode="refinement")
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))
    monkeypatch.setattr(extraction, "_resolve_ambiguous_conflict", AsyncMock(return_value="refinement"))

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls if sql]
    assert not any("SET predicate = $1" in sql for sql in statements)
    assert not any("SET valid_to = now()" in sql for sql in statements)
    assert any("INSERT INTO memory_edges" in sql for sql in statements)


@pytest.mark.asyncio
async def test_graph_writes_source_level_node_and_edge_attribute_snapshots(monkeypatch):
    conn = _ExtractionConnection(mode="new")
    _install_db(monkeypatch, conn)
    monkeypatch.setattr(memory_repo, "_get_embedding", AsyncMock(return_value=[0.1, 0.2]))

    result = await extraction._upsert_graph(
        42,
        _graph(),
        "key",
        source_memory_id=77,
        expected_epoch=9,
    )

    assert result == 1
    statements = [(" ".join(sql.split()), args) for _, sql, args in conn.calls if sql]
    node_source_sql, node_source_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_node_sources" in sql
    )
    assert "attributes_complete" in node_source_sql
    assert "ON CONFLICT (node_id, memory_id)" in node_source_sql
    assert "77" in repr(node_source_args)

    edge_source_sql, edge_source_args = next(
        (sql, args) for sql, args in statements if "INSERT INTO memory_edge_sources" in sql
    )
    for attribute in ("predicate", "predicate_embedding", "weight", "is_core", "attributes_complete"):
        assert attribute in edge_source_sql
    assert "77" in repr(edge_source_args)

"""Unit contracts for transaction-scoped LTM graph persistence."""

from __future__ import annotations

import pytest

from app.repos.memory_graph_writer import (
    GraphConflictClosure,
    GraphEdgeCandidate,
    GraphMutationPlan,
    GraphNodeCandidate,
    write_graph,
)


class RecordingConnection:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        similar_nodes: dict[str, str] | None = None,
    ):
        self.calls: list[tuple[str, str, tuple]] = []
        self.fail_on = fail_on
        self.similar_nodes = similar_nodes or {}
        self.next_edge_id = 500

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        normalized = " ".join(sql.split())
        if "SELECT input.input_name" in normalized:
            return [
                {"input_name": input_name, "entity_name": entity_name}
                for input_name, entity_name in self.similar_nodes.items()
            ]
        if "INSERT INTO memory_nodes" in normalized:
            names = args[1]
            return [{"id": 100 + index, "entity_name": name} for index, name in enumerate(names, start=1)]
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        if "INSERT INTO memory_edges" in sql:
            self.next_edge_id += 1
            return {"id": self.next_edge_id}
        return None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("injected writer failure")
        return "OK"


def _node(name: str, *, sources=frozenset({77})) -> GraphNodeCandidate:
    return GraphNodeCandidate(
        name=name,
        entity_type="person" if name == "Alice" else "skill",
        description="user" if name == "Alice" else "language",
        embedding=(0.1, 0.2),
        wing="knowledge",
        room="facts",
        source_memory_ids=sources,
    )


def _plan(*, closure: GraphConflictClosure | None = None) -> GraphMutationPlan:
    return GraphMutationPlan(
        nodes=(_node("Alice"), _node("Python")),
        edges=(
            GraphEdgeCandidate(
                source_name="Alice",
                target_name="Python",
                predicate="uses",
                predicate_embedding=(0.3, 0.4),
                weight=0.8,
                is_core=False,
                source_memory_ids=frozenset({77}),
                close_conflicts=(closure,) if closure else (),
            ),
        ),
    )


def test_graph_candidates_require_durable_provenance() -> None:
    with pytest.raises(ValueError, match="source_memory_ids"):
        _node("Alice", sources=frozenset())

    with pytest.raises(ValueError, match="source_memory_ids"):
        GraphEdgeCandidate(
            source_name="Alice",
            target_name="Python",
            predicate="uses",
            predicate_embedding=None,
            weight=0.8,
            is_core=False,
            source_memory_ids=frozenset(),
        )


@pytest.mark.parametrize("weight", [-0.1, 1.1, float("inf"), float("nan")])
def test_graph_edges_reject_invalid_weights(weight: float) -> None:
    with pytest.raises(ValueError, match="weight"):
        GraphEdgeCandidate(
            source_name="Alice",
            target_name="Python",
            predicate="uses",
            predicate_embedding=None,
            weight=weight,
            is_core=False,
            source_memory_ids=frozenset({77}),
        )


def test_graph_candidates_reject_boolean_source_ids() -> None:
    with pytest.raises(ValueError, match="source_memory_ids"):
        _node("Alice", sources=frozenset({True}))


@pytest.mark.asyncio
async def test_writer_persists_provenance_before_projecting_current_attributes() -> None:
    conn = RecordingConnection()

    result = await write_graph(conn, 42, _plan())

    assert result.edges_written == 1
    assert result.node_ids == {"Alice": 101, "Python": 102}

    statements = [" ".join(sql.split()) for _, sql, _ in conn.calls]
    node_source_index = next(i for i, sql in enumerate(statements) if "INSERT INTO memory_node_sources" in sql)
    node_projection_index = next(i for i, sql in enumerate(statements) if "UPDATE memory_nodes AS node" in sql)
    edge_source_index = next(i for i, sql in enumerate(statements) if "INSERT INTO memory_edge_sources" in sql)
    edge_projection_index = next(i for i, sql in enumerate(statements) if "UPDATE memory_edges AS edge" in sql)

    assert node_source_index < node_projection_index < edge_source_index < edge_projection_index


@pytest.mark.asyncio
async def test_writer_maps_semantically_equivalent_input_to_canonical_node() -> None:
    conn = RecordingConnection(similar_nodes={"Alice": "Алиса"})

    result = await write_graph(conn, 42, _plan())

    assert result.node_ids == {"Alice": 101, "Python": 102}
    node_insert_args = next(args for _, sql, args in conn.calls if "INSERT INTO memory_nodes" in sql)
    assert node_insert_args[1] == ["Алиса", "Python"]


@pytest.mark.asyncio
async def test_writer_closes_only_guarded_conflict_selected_by_caller() -> None:
    conn = RecordingConnection()
    closure = GraphConflictClosure(edge_id=9, predicate="works with")

    await write_graph(conn, 42, _plan(closure=closure))

    close_calls = [
        (" ".join(sql.split()), args)
        for kind, sql, args in conn.calls
        if kind == "execute" and "SET valid_to = now()" in sql
    ]
    assert len(close_calls) == 1
    close_sql, close_args = close_calls[0]
    assert "source_node = $3" in close_sql
    assert "target_node = $4" in close_sql
    assert "predicate = $5" in close_sql
    assert close_args == (9, 42, 101, 102, "works with")


@pytest.mark.asyncio
async def test_writer_propagates_database_failure_to_caller_transaction() -> None:
    conn = RecordingConnection(fail_on="INSERT INTO memory_edge_sources")

    with pytest.raises(RuntimeError, match="injected writer failure"):
        await write_graph(conn, 42, _plan())

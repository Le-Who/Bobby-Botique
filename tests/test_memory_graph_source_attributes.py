"""The writer must not copy one memory's edge attributes onto another source.

These tests inspect the real writer's SQL-boundary payloads. PostgreSQL source
deletion and projection triggers remain the responsibility of integration tests.
"""

from dataclasses import replace

import pytest

from app.repos.memory_graph_writer import GraphEdgeCandidate, GraphMutationPlan, GraphNodeCandidate, write_graph


class GraphConnection:
    """Record mutations while resolving known synthetic node/edge identities."""

    def __init__(self):
        self.edge_inserts = []
        self.source_rows = {}
        self.edge_projections = []

    async def fetch(self, sql, *args):
        if "SELECT input.input_name" in sql:
            assert args[0] == 42
            return [{"input_name": "Alias", "entity_name": "Alice"}] if "Alias" in args[1] else []
        assert "INSERT INTO memory_nodes" in sql
        assert args[0] == 42
        assert args[1] == ["Alice", "Python"]
        return [{"id": 101, "entity_name": "Alice"}, {"id": 102, "entity_name": "Python"}]

    async def fetchrow(self, sql, *args):
        assert "INSERT INTO memory_edges" in sql
        self.edge_inserts.append(args)
        return {"id": 501}

    async def execute(self, sql, *args):
        if "INSERT INTO memory_edge_sources" in sql:
            assert args[2] == 42
            assert len(args) == 7
            for edge_id, source_id, predicate, embedding, weight, is_core in zip(
                args[0], args[1], args[3], args[4], args[5], args[6], strict=True
            ):
                assert edge_id == 501
                assert source_id not in self.source_rows
                self.source_rows[source_id] = (predicate, embedding, weight, is_core)
        elif "UPDATE memory_edges AS edge" in sql:
            self.edge_projections.append(args)
        else:
            assert "INSERT INTO memory_node_sources" in sql or "UPDATE memory_nodes AS node" in sql
        return "OK"


def _node(name, sources):
    return GraphNodeCandidate(
        name=name,
        entity_type="person" if name != "Python" else "skill",
        description="synthetic graph fixture",
        embedding=(0.1, 0.2),
        wing="knowledge",
        room="facts",
        source_memory_ids=frozenset(sources),
    )


def _edge(*, source="Alice", sources=(77,), weight=0.2, core=False, embedding=(0.3, 0.4)):
    return GraphEdgeCandidate(
        source_name=source,
        target_name="Python",
        predicate="uses",
        predicate_embedding=embedding,
        weight=weight,
        is_core=core,
        source_memory_ids=frozenset(sources),
    )


def _plan(edges, *, alias=False):
    nodes = (_node("Alice", (77, 88, 99)), _node("Python", (77, 88, 99)))
    if alias:
        nodes += (_node("Alias", (88, 99)),)
    return GraphMutationPlan(nodes=nodes, edges=tuple(edges))


def _assert_aggregate(conn, result, sources):
    assert result.node_ids["Alice"] == 101
    assert result.node_ids["Python"] == 102
    assert result.edges_written == 1
    assert result.affected_edge_ids == (501,)
    assert len(conn.edge_inserts) == 1
    assert conn.edge_inserts[0][:4] == (42, 101, 102, "uses")
    assert conn.edge_inserts[0][5:] == (0.9, True, sources)
    assert conn.edge_projections == [([501], 42)]


@pytest.mark.parametrize("alias", [False, True], ids=["exact", "semantic-alias"])
@pytest.mark.parametrize("reverse", [False, True], ids=["weak-first", "strong-first"])
async def test_duplicate_edge_keeps_each_sources_own_attributes(alias, reverse):
    # A max/OR merge before provenance serialization would strengthen source 77
    # using source 88's evidence, even after 88 is deleted.
    edges = [
        _edge(),
        _edge(source="Alias" if alias else "Alice", sources=(88,), weight=0.9, core=True, embedding=(0.8, 0.9)),
    ]
    if reverse:
        edges.reverse()
    conn = GraphConnection()

    result = await write_graph(conn, 42, _plan(edges, alias=alias))

    assert conn.source_rows == {
        77: ("uses", "[0.3,0.4]", 0.2, False),
        88: ("uses", "[0.8,0.9]", 0.9, True),
    }
    _assert_aggregate(conn, result, [77, 88])
    if alias:
        assert result.node_ids["Alias"] == 101


@pytest.mark.parametrize("alias", [False, True], ids=["exact", "semantic-alias"])
async def test_overlapping_support_merges_only_attributes_for_the_shared_source(alias):
    conn = GraphConnection()
    edges = [
        _edge(sources=(77, 99)),
        _edge(source="Alias" if alias else "Alice", sources=(88, 99), weight=0.9, core=True),
    ]

    result = await write_graph(conn, 42, _plan(edges, alias=alias))

    assert conn.source_rows == {
        77: ("uses", "[0.3,0.4]", 0.2, False),
        88: ("uses", "[0.3,0.4]", 0.9, True),
        99: ("uses", "[0.3,0.4]", 0.9, True),
    }
    _assert_aggregate(conn, result, [77, 88, 99])


@pytest.mark.parametrize("reverse", [False, True], ids=["core-first", "weight-first"])
async def test_same_source_duplicate_retains_max_weight_and_core_without_duplicate_rows(reverse):
    conn = GraphConnection()
    core_edge = _edge(core=True)
    weighted_edge = replace(core_edge, weight=0.9, is_core=False)
    edges = [core_edge, weighted_edge]
    if reverse:
        edges.reverse()

    result = await write_graph(conn, 42, _plan(edges))

    assert conn.source_rows == {77: ("uses", "[0.3,0.4]", 0.9, True)}
    _assert_aggregate(conn, result, [77])

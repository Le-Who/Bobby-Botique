"""Deep transaction-scoped persistence boundary for the private LTM graph.

Callers retain ownership of privacy checks, RLS context, advisory locking, and
the database transaction.  This module receives an already-bound connection
and performs only deterministic node, edge, and provenance mutations.  It does
not acquire the global pool and does not call external services.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Any

SEMANTIC_NODE_DISTANCE = 0.12


def _require_source_ids(source_ids: frozenset[int]) -> None:
    if not source_ids or any(type(source_id) is not int or source_id <= 0 for source_id in source_ids):
        raise ValueError("source_memory_ids must contain positive durable memory IDs")


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _halfvec(embedding: tuple[float, ...] | None) -> str | None:
    if embedding is None:
        return None
    return f"[{','.join(str(value) for value in embedding)}]"


@dataclass(frozen=True, slots=True)
class GraphConflictClosure:
    """A caller-approved current edge that may be closed during mutation."""

    edge_id: int
    predicate: str

    def __post_init__(self) -> None:
        if type(self.edge_id) is not int or self.edge_id <= 0:
            raise ValueError("edge_id must be positive")
        _require_text(self.predicate, "predicate")


@dataclass(frozen=True, slots=True)
class GraphNodeCandidate:
    """Normalized node attributes and their durable provenance anchors."""

    name: str
    entity_type: str
    description: str
    embedding: tuple[float, ...] | None
    wing: str
    room: str
    source_memory_ids: frozenset[int]

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.entity_type, "entity_type")
        _require_text(self.wing, "wing")
        _require_source_ids(self.source_memory_ids)


@dataclass(frozen=True, slots=True)
class GraphEdgeCandidate:
    """Normalized exact-predicate edge and its durable provenance anchors."""

    source_name: str
    target_name: str
    predicate: str
    predicate_embedding: tuple[float, ...] | None
    weight: float
    is_core: bool
    source_memory_ids: frozenset[int]
    close_conflicts: tuple[GraphConflictClosure, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.source_name, "source_name")
        _require_text(self.target_name, "target_name")
        _require_text(self.predicate, "predicate")
        _require_source_ids(self.source_memory_ids)
        if not isfinite(self.weight) or not 0.0 <= self.weight <= 1.0:
            raise ValueError("weight must be a finite value between 0.0 and 1.0")


@dataclass(frozen=True, slots=True)
class GraphMutationPlan:
    """Complete graph mutation prepared outside the write transaction."""

    nodes: tuple[GraphNodeCandidate, ...]
    edges: tuple[GraphEdgeCandidate, ...]

    def __post_init__(self) -> None:
        node_names = {node.name for node in self.nodes}
        for edge in self.edges:
            if edge.source_name not in node_names or edge.target_name not in node_names:
                raise ValueError("every graph edge endpoint must have a node candidate")


@dataclass(frozen=True, slots=True)
class GraphMutationResult:
    """Identifiers and counts produced by one graph mutation."""

    node_ids: dict[str, int]
    affected_node_ids: tuple[int, ...]
    affected_edge_ids: tuple[int, ...]
    edges_written: int


@dataclass(slots=True)
class _PreparedEdge:
    source_id: int
    target_id: int
    predicate: str
    predicate_embedding: str | None
    weight: float
    is_core: bool
    source_memory_ids: set[int]
    close_conflicts: set[GraphConflictClosure]


async def _resolve_and_upsert_nodes(
    conn: Any,
    user_id: int,
    candidates: tuple[GraphNodeCandidate, ...],
) -> tuple[dict[str, int], tuple[int, ...]]:
    if not candidates:
        return {}, ()

    name_mapping = {candidate.name: candidate.name for candidate in candidates}
    embedded_candidates = [candidate for candidate in candidates if candidate.embedding is not None]
    if embedded_candidates:
        similar_nodes = await conn.fetch(
            """
            SELECT input.input_name, node.entity_name
            FROM unnest($2::text[], $3::halfvec[]) AS input(input_name, embedding)
            LEFT JOIN LATERAL (
                SELECT entity_name
                FROM memory_nodes
                WHERE user_id = $1
                  AND embedding IS NOT NULL
                  AND embedding <=> input.embedding < $4
                ORDER BY embedding <=> input.embedding ASC
                LIMIT 1
            ) AS node ON true
            WHERE node.entity_name IS NOT NULL
            """,
            user_id,
            [candidate.name for candidate in embedded_candidates],
            [_halfvec(candidate.embedding) for candidate in embedded_candidates],
            SEMANTIC_NODE_DISTANCE,
        )
        for row in similar_nodes:
            name_mapping[str(row["input_name"])] = str(row["entity_name"])

    canonical_candidates: dict[str, GraphNodeCandidate] = {}
    for candidate in candidates:
        canonical_name = name_mapping[candidate.name]
        previous = canonical_candidates.get(canonical_name)
        if previous is None or len(candidate.description) > len(previous.description):
            canonical_candidates[canonical_name] = candidate

    canonical_names = list(canonical_candidates)
    node_rows = await conn.fetch(
        """
        INSERT INTO memory_nodes
            (user_id, entity_name, entity_type, description, embedding, wing, room)
        SELECT $1, *
        FROM unnest(
            $2::text[], $3::text[], $4::text[], $5::halfvec[], $6::text[], $7::text[]
        )
        ON CONFLICT (user_id, entity_name)
        DO UPDATE SET updated_at = now()
        RETURNING id, entity_name
        """,
        user_id,
        canonical_names,
        [canonical_candidates[name].entity_type for name in canonical_names],
        [canonical_candidates[name].description for name in canonical_names],
        [_halfvec(canonical_candidates[name].embedding) for name in canonical_names],
        [canonical_candidates[name].wing for name in canonical_names],
        [canonical_candidates[name].room for name in canonical_names],
    )
    canonical_ids = {str(row["entity_name"]): int(row["id"]) for row in node_rows}
    missing_names = set(canonical_names) - canonical_ids.keys()
    if missing_names:
        raise RuntimeError(f"graph node upsert returned no IDs for: {sorted(missing_names)!r}")

    node_ids = {name: canonical_ids[canonical] for name, canonical in name_mapping.items()}
    source_snapshots: dict[tuple[int, int], GraphNodeCandidate] = {}
    for candidate in candidates:
        node_id = node_ids[candidate.name]
        for source_memory_id in candidate.source_memory_ids:
            snapshot_key = (node_id, source_memory_id)
            previous = source_snapshots.get(snapshot_key)
            if previous is None or len(candidate.description) > len(previous.description):
                source_snapshots[snapshot_key] = candidate

    ordered_sources = sorted(source_snapshots)
    await conn.execute(
        """
        INSERT INTO memory_node_sources
            (node_id, memory_id, user_id, entity_type, description,
             embedding, wing, room, attributes_complete)
        SELECT snapshot.node_id, snapshot.memory_id, $3,
               snapshot.entity_type, snapshot.description, snapshot.embedding,
               snapshot.wing, snapshot.room, TRUE
        FROM unnest(
            $1::bigint[], $2::bigint[], $4::text[], $5::text[],
            $6::halfvec[], $7::text[], $8::text[]
        ) AS snapshot(
            node_id, memory_id, entity_type, description, embedding, wing, room
        )
        ON CONFLICT (node_id, memory_id) DO UPDATE SET
            entity_type = EXCLUDED.entity_type,
            description = EXCLUDED.description,
            embedding = EXCLUDED.embedding,
            wing = EXCLUDED.wing,
            room = EXCLUDED.room,
            attributes_complete = TRUE,
            created_at = now()
        """,
        [key[0] for key in ordered_sources],
        [key[1] for key in ordered_sources],
        user_id,
        [source_snapshots[key].entity_type for key in ordered_sources],
        [source_snapshots[key].description for key in ordered_sources],
        [_halfvec(source_snapshots[key].embedding) for key in ordered_sources],
        [source_snapshots[key].wing for key in ordered_sources],
        [source_snapshots[key].room for key in ordered_sources],
    )

    affected_node_ids = tuple(sorted({key[0] for key in ordered_sources}))
    await conn.execute(
        """
        WITH target_nodes AS (
            SELECT unnest($1::bigint[]) AS node_id
        ), base_attributes AS (
            SELECT DISTINCT ON (source.node_id)
                   source.node_id, source.entity_type, source.description,
                   source.embedding, source.wing, source.room
            FROM memory_node_sources AS source
            JOIN target_nodes AS target ON target.node_id = source.node_id
            WHERE source.user_id = $2
              AND source.attributes_complete IS TRUE
            ORDER BY source.node_id, source.created_at DESC, source.memory_id DESC
        ), media_attributes AS (
            SELECT DISTINCT ON (source.node_id)
                   source.node_id, source.file_id, source.file_type
            FROM memory_node_sources AS source
            JOIN target_nodes AS target ON target.node_id = source.node_id
            WHERE source.user_id = $2
              AND source.attributes_complete IS TRUE
              AND source.file_id IS NOT NULL
            ORDER BY source.node_id, source.created_at DESC, source.memory_id DESC
        )
        UPDATE memory_nodes AS node
        SET entity_type = base.entity_type,
            description = base.description,
            embedding = base.embedding,
            wing = base.wing,
            room = base.room,
            file_id = media.file_id,
            file_type = media.file_type,
            updated_at = now()
        FROM base_attributes AS base
        LEFT JOIN media_attributes AS media ON media.node_id = base.node_id
        WHERE node.id = base.node_id
          AND node.user_id = $2
        """,
        list(affected_node_ids),
        user_id,
    )
    return node_ids, affected_node_ids


def _prepare_edges(
    candidates: tuple[GraphEdgeCandidate, ...],
    node_ids: dict[str, int],
) -> list[_PreparedEdge]:
    prepared: dict[tuple[int, int, str], _PreparedEdge] = {}
    for candidate in candidates:
        key = (node_ids[candidate.source_name], node_ids[candidate.target_name], candidate.predicate)
        current = prepared.get(key)
        if current is None:
            prepared[key] = _PreparedEdge(
                source_id=key[0],
                target_id=key[1],
                predicate=key[2],
                predicate_embedding=_halfvec(candidate.predicate_embedding),
                weight=candidate.weight,
                is_core=candidate.is_core,
                source_memory_ids=set(candidate.source_memory_ids),
                close_conflicts=set(candidate.close_conflicts),
            )
            continue

        current.weight = max(current.weight, candidate.weight)
        current.is_core = current.is_core or candidate.is_core
        current.source_memory_ids.update(candidate.source_memory_ids)
        current.close_conflicts.update(candidate.close_conflicts)
    return list(prepared.values())


async def _upsert_edges(
    conn: Any,
    user_id: int,
    candidates: tuple[GraphEdgeCandidate, ...],
    node_ids: dict[str, int],
) -> tuple[tuple[int, ...], int]:
    prepared_edges = _prepare_edges(candidates, node_ids)
    if not prepared_edges:
        return (), 0

    edge_snapshots: dict[int, _PreparedEdge] = {}
    for edge in prepared_edges:
        for conflict in sorted(edge.close_conflicts, key=lambda item: (item.edge_id, item.predicate)):
            await conn.execute(
                """
                UPDATE memory_edges
                SET valid_to = now()
                WHERE id = $1
                  AND user_id = $2
                  AND source_node = $3
                  AND target_node = $4
                  AND predicate = $5
                  AND valid_to IS NULL
                """,
                conflict.edge_id,
                user_id,
                edge.source_id,
                edge.target_id,
                conflict.predicate,
            )

        row = await conn.fetchrow(
            """
            INSERT INTO memory_edges
                (user_id, source_node, target_node, predicate,
                 predicate_embedding, weight, is_core, source_memory_ids, valid_from)
            VALUES ($1, $2, $3, $4, $5::halfvec, $6, $7, $8::bigint[], now())
            ON CONFLICT (user_id, source_node, target_node, predicate) WHERE valid_to IS NULL
            DO UPDATE SET updated_at = now()
            RETURNING id
            """,
            user_id,
            edge.source_id,
            edge.target_id,
            edge.predicate,
            edge.predicate_embedding,
            edge.weight,
            edge.is_core,
            sorted(edge.source_memory_ids),
        )
        if not row:
            raise RuntimeError("graph edge upsert returned no id")
        edge_id = int(row["id"])
        previous = edge_snapshots.get(edge_id)
        if previous is None:
            edge_snapshots[edge_id] = edge
        else:
            previous.weight = max(previous.weight, edge.weight)
            previous.is_core = previous.is_core or edge.is_core
            previous.source_memory_ids.update(edge.source_memory_ids)

    ordered_sources = sorted(
        (edge_id, source_memory_id)
        for edge_id, snapshot in edge_snapshots.items()
        for source_memory_id in snapshot.source_memory_ids
    )
    await conn.execute(
        """
        INSERT INTO memory_edge_sources
            (edge_id, memory_id, user_id, predicate, predicate_embedding,
             weight, is_core, attributes_complete)
        SELECT snapshot.edge_id, snapshot.memory_id, $3,
               snapshot.predicate, snapshot.predicate_embedding,
               snapshot.weight, snapshot.is_core, TRUE
        FROM unnest(
            $1::bigint[], $2::bigint[], $4::text[], $5::halfvec[],
            $6::double precision[], $7::boolean[]
        ) AS snapshot(
            edge_id, memory_id, predicate, predicate_embedding, weight, is_core
        )
        ON CONFLICT (edge_id, memory_id) DO UPDATE SET
            predicate = EXCLUDED.predicate,
            predicate_embedding = EXCLUDED.predicate_embedding,
            weight = EXCLUDED.weight,
            is_core = EXCLUDED.is_core,
            attributes_complete = TRUE,
            created_at = now()
        """,
        [key[0] for key in ordered_sources],
        [key[1] for key in ordered_sources],
        user_id,
        [edge_snapshots[key[0]].predicate for key in ordered_sources],
        [edge_snapshots[key[0]].predicate_embedding for key in ordered_sources],
        [edge_snapshots[key[0]].weight for key in ordered_sources],
        [edge_snapshots[key[0]].is_core for key in ordered_sources],
    )

    affected_edge_ids = tuple(sorted(edge_snapshots))
    await conn.execute(
        """
        WITH target_edges AS (
            SELECT unnest($1::bigint[]) AS edge_id
        ), aggregate_attributes AS (
            SELECT source.edge_id, MAX(source.weight) AS weight,
                   BOOL_OR(source.is_core) AS is_core,
                   ARRAY_AGG(source.memory_id ORDER BY source.memory_id) AS memory_ids
            FROM memory_edge_sources AS source
            JOIN target_edges AS target ON target.edge_id = source.edge_id
            WHERE source.user_id = $2
              AND source.attributes_complete IS TRUE
            GROUP BY source.edge_id
        ), winning_predicate AS (
            SELECT DISTINCT ON (source.edge_id)
                   source.edge_id, source.predicate, source.predicate_embedding
            FROM memory_edge_sources AS source
            JOIN target_edges AS target ON target.edge_id = source.edge_id
            WHERE source.user_id = $2
              AND source.attributes_complete IS TRUE
            ORDER BY source.edge_id, source.created_at DESC, source.memory_id DESC
        )
        UPDATE memory_edges AS edge
        SET predicate = winner.predicate,
            predicate_embedding = winner.predicate_embedding,
            weight = aggregate.weight,
            is_core = aggregate.is_core,
            source_memory_ids = aggregate.memory_ids,
            updated_at = now()
        FROM aggregate_attributes AS aggregate
        JOIN winning_predicate AS winner ON winner.edge_id = aggregate.edge_id
        WHERE edge.id = aggregate.edge_id
          AND edge.user_id = $2
        """,
        list(affected_edge_ids),
        user_id,
    )
    return affected_edge_ids, len(prepared_edges)


async def write_graph(conn: Any, user_id: int, plan: GraphMutationPlan) -> GraphMutationResult:
    """Persist one provenance-backed graph plan using the caller's transaction."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("user_id must be positive")

    node_ids, affected_node_ids = await _resolve_and_upsert_nodes(conn, user_id, plan.nodes)
    affected_edge_ids, edges_written = await _upsert_edges(conn, user_id, plan.edges, node_ids)
    return GraphMutationResult(
        node_ids=node_ids,
        affected_node_ids=affected_node_ids,
        affected_edge_ids=affected_edge_ids,
        edges_written=edges_written,
    )


__all__ = [
    "GraphConflictClosure",
    "GraphEdgeCandidate",
    "GraphMutationPlan",
    "GraphMutationResult",
    "GraphNodeCandidate",
    "SEMANTIC_NODE_DISTANCE",
    "write_graph",
]

"""Public recall keeps one durable consent generation through every phase."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.repos import memory, memory_consent, memory_tools


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        assert not self.connection.in_transaction
        self.connection.in_transaction = True
        self.connection.read_locked = False
        return self.connection

    async def __aexit__(self, *_args):
        self.connection.in_transaction = False
        self.connection.read_locked = False
        boundary = self.connection.boundary
        if self.connection.candidates_fetched and boundary.pause_candidates:
            boundary.candidates_ready.set()
            await boundary.resume_candidates.wait()


class _Connection:
    def __init__(self, boundary):
        self.boundary = boundary
        self.in_transaction = False
        self.read_locked = False
        self.candidates_fetched = False

    def transaction(self):
        return _Transaction(self)

    async def execute(self, _sql, *_args):
        assert self.in_transaction


class _Acquisition:
    def __init__(self, boundary):
        self.connection = _Connection(boundary)

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        assert not self.connection.in_transaction


class _Pool:
    def __init__(self, boundary):
        self.boundary = boundary

    def acquire(self):
        return _Acquisition(self.boundary)


class _RecallBoundary:
    """Synthetic SQL/provider boundary; production recall and leases stay real.

    Chat predicates are interpreted from emitted SQL so dropping either the
    epoch or blocked predicate admits rows and breaks the rejection oracles.
    Private rows are deliberately available even after a generation change.
    """

    def __init__(self):
        self.epoch = 7
        self.enabled = True
        self.blocked = False
        self.account_exists = True
        self.leases = {}
        self.acquired_epochs = []
        self.resolved_epochs = []
        self.gate_epochs = []
        self.private_reads = []
        self.embeddings = []
        self.expansions = []
        self.judge_prompts = []
        self.reservations = []
        self.embedding_started = asyncio.Event()
        self.resume_embedding = asyncio.Event()
        self.pause_embedding_number = None
        self.graph_embedding_failure = None
        self.expansion_started = asyncio.Event()
        self.resume_expansion = asyncio.Event()
        self.pause_expansion = False
        self.candidates_ready = asyncio.Event()
        self.resume_candidates = asyncio.Event()
        self.pause_candidates = False
        self.created_at = datetime(2026, 10, 9, tzinfo=UTC)

    def _chat_matches(self, sql, params):
        if "JOIN public.users" in sql and not self.account_exists:
            return False
        if "private_data_blocked IS FALSE" in sql and self.blocked:
            return False
        epoch_match = re.search(r"(?:chat\.)?memory_epoch\s*=\s*\$(\d+)", sql)
        if epoch_match:
            epoch = params[int(epoch_match[1]) - 1]
            self.gate_epochs.append(epoch)
            if epoch is not None and epoch != self.epoch:
                return False
        return True

    async def context(self, _user_id=None, _admin=False, *, conn):
        assert conn.in_transaction

    async def query(self, sql, params=(), *, conn=None):
        normalized = " ".join(sql.split())
        assert conn is not None and conn.in_transaction
        if "INSERT INTO public.private_data_leases" in normalized:
            self.acquired_epochs.append(params[3])
            if not self._chat_matches(normalized, params) or not self.enabled:
                return []
            self.leases[params[1]] = (params[0], params[3])
            return [{"lease_id": params[1]}]
        if "DELETE FROM public.private_data_leases" in normalized:
            if self.leases.get(params[1], (None,))[0] == params[0]:
                self.leases.pop(params[1], None)
            return []
        if "FROM chats" in normalized or "FROM public.chats" in normalized:
            if not self._chat_matches(normalized, params):
                return []
            row = {"memory_epoch": self.epoch, "ltm_enabled": self.enabled, "private_data_blocked": self.blocked}
            if "SELECT chat.memory_epoch" in normalized and "memory_epoch =" not in normalized:
                self.resolved_epochs.append(self.epoch)
            if "FOR SHARE" in normalized:
                conn.read_locked = self.enabled
            return [row]
        assert conn.read_locked, "private rows require the caller's transactional consent lock"
        if "live_node_sources" in normalized:
            self.private_reads.append("nodes")
            return [{"id": 11, "entity_name": "Example", "entity_type": "concept", "description": "safe", "sim": 0.9}]
        if "live_edge_sources" in normalized:
            self.private_reads.append("edges")
            return [
                {
                    "from_name": "Example",
                    "predicate": "USES",
                    "to_name": "Python",
                    "edge_id": 77,
                    "source_memory_ids": [731],
                    "effective_weight": 1.0,
                    "weight": 1.0,
                    "is_core": True,
                    "hop": 1,
                }
            ]
        if "WITH live_sources" in normalized:
            self.private_reads.append("temporal")
            return []
        if "SELECT id, content" in normalized and "ANY(" in normalized:
            self.private_reads.append("passages")
            return [{"id": 731, "content": "Synthetic source passage"}]
        if "FROM long_term_memory" in normalized:
            self.private_reads.append("memories")
            conn.candidates_fetched = True
            return [
                {
                    "id": 731,
                    "content": "Synthetic candidate fact",
                    "source_type": "user_message",
                    "metadata": {},
                    "created_at": self.created_at,
                    "sim": 0.9,
                    "rlhf_neg": 0,
                }
            ]
        raise AssertionError(f"Unexpected SQL boundary: {normalized[:80]}")

    async def embedding(self, text, _key, *, task_type):
        self.embeddings.append((text, task_type))
        if len(self.embeddings) == self.pause_embedding_number:
            self.embedding_started.set()
            await self.resume_embedding.wait()
        if len(self.embeddings) == 2:
            if self.graph_embedding_failure == "none":
                return None
            if self.graph_embedding_failure == "error":
                raise RuntimeError("Synthetic graph embedding failure")
        return [0.1, 0.2]

    async def expand(self, text, _key):
        self.expansions.append(text)
        if self.pause_expansion:
            self.expansion_started.set()
            await self.resume_expansion.wait()
        return "expanded synthetic query"

    async def reserve(self, key_hash, model):
        self.reservations.append((key_hash, model))
        return True

    async def override(self, *_args, **_kwargs):
        return None

    async def generate(self, **kwargs):
        self.judge_prompts.append(kwargs["contents"])
        return SimpleNamespace(text='[{"index":0,"relevant":true}]')

    async def observe(self, awaitable, **_kwargs):
        return await awaitable

    def unblock(self):
        self.resume_embedding.set()
        self.resume_expansion.set()
        self.resume_candidates.set()


@pytest.fixture
def recall_boundary(monkeypatch):
    from app.observability import workload_events
    from app.repos import keys

    boundary = _RecallBoundary()
    manager = SimpleNamespace(pool=_Pool(boundary), is_connected=True)
    for module in (memory, memory_consent):
        monkeypatch.setattr(module, "db_manager", manager)
        monkeypatch.setattr(module, "db_query", boundary.query)
        monkeypatch.setattr(module, "set_user_context", boundary.context)
    monkeypatch.setattr(memory_consent, "clear_user_context", boundary.context)
    monkeypatch.setattr(memory, "_get_embedding", boundary.embedding)
    monkeypatch.setattr(memory, "_trgm_available", False)
    monkeypatch.setattr(memory, "expand_query_with_llm", boundary.expand)
    monkeypatch.setattr(memory, "run_gemini_override", boundary.override)
    monkeypatch.setattr(keys, "reserve_gemini_key_usage", boundary.reserve)
    monkeypatch.setattr(workload_events, "observe_workload_call", boundary.observe)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=boundary.generate)))
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _key: client)
    token = memory._current_retrieved_edge_ids.set((42, (999,)))
    yield boundary
    boundary.unblock()
    memory._current_retrieved_edge_ids.reset(token)
    assert boundary.leases == {}, "every public retrieval must finish its own lease"


async def _finish_task(task, boundary):
    boundary.unblock()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_epoch", "blocked", "missing_account"])
@pytest.mark.parametrize("consent_checked", [False, True])
async def test_public_vector_rejects_invalidated_snapshot_at_read_transaction(recall_boundary, change, consent_checked):
    """Dropping either SQL consent predicate permits a private read after embedding."""
    boundary = recall_boundary
    boundary.pause_embedding_number = 1
    task = asyncio.create_task(
        memory.search_memories(42, "safe query", "synthetic-key", expected_epoch=7, _consent_checked=consent_checked)
    )
    try:
        await boundary.embedding_started.wait()
        if change == "new_epoch":
            boundary.epoch = 9
        elif change == "blocked":
            boundary.blocked = True
        else:
            boundary.account_exists = False
        boundary.resume_embedding.set()
        assert await task == []
        assert boundary.private_reads == []
        assert boundary.acquired_epochs == [7]
        assert boundary.resolved_epochs == []
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
async def test_public_vector_returns_same_generation_fact(recall_boundary):
    boundary = recall_boundary
    result = await memory.search_memories(42, "safe query", "synthetic-key", expected_epoch=7)
    assert result == [
        {
            "id": 731,
            "content": "Synthetic candidate fact",
            "similarity": 0.9,
            "source_type": "user_message",
            "created_at": datetime(2026, 10, 9, tzinfo=UTC),
        }
    ]
    assert boundary.private_reads == ["memories"]
    assert boundary.acquired_epochs == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["expansion", "vector_embedding", "candidate_retrieval", "graph_embedding"])
async def test_public_graph_keeps_epoch_across_suspended_phases(recall_boundary, phase):
    """Rebinding nested recall or removing graph revalidation exposes a stale graph."""
    boundary = recall_boundary
    boundary.pause_expansion = phase == "expansion"
    boundary.pause_embedding_number = {"vector_embedding": 1, "graph_embedding": 2}.get(phase)
    boundary.pause_candidates = phase == "candidate_retrieval"

    async def recall():
        result = await memory.search_memories_with_graph(
            42, "Tell me about my project", "synthetic-key", expected_epoch=7
        )
        return result, memory.get_current_retrieved_edge_ids(42)

    task = asyncio.create_task(recall())
    try:
        started = {
            "expansion": boundary.expansion_started,
            "candidate_retrieval": boundary.candidates_ready,
        }.get(phase, boundary.embedding_started)
        await started.wait()
        boundary.epoch = 9
        boundary.unblock()
        result, attribution = await task
        assert result == ([], [], {})
        assert attribution == []
        assert all(epoch == 7 for epoch in boundary.acquired_epochs)
        assert boundary.resolved_epochs == []
        if phase in {"expansion", "vector_embedding"}:
            assert boundary.private_reads == []
            assert len(boundary.embeddings) == (0 if phase == "expansion" else 1)
        else:
            assert boundary.private_reads == ["memories"]
            assert len(boundary.embeddings) == (1 if phase == "candidate_retrieval" else 2)
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
async def test_public_graph_same_generation_returns_provenance_and_attribution(recall_boundary):
    boundary = recall_boundary
    memories, triples, passages = await memory.search_memories_with_graph(
        42, "safe query", "synthetic-key", expected_epoch=7
    )
    assert [row["id"] for row in memories] == [731]
    assert triples == ["Example — USES → Python ★"]
    assert passages == {"Example — USES → Python": "Synthetic source passage"}
    assert memory.get_current_retrieved_edge_ids(42) == [77]
    assert boundary.acquired_epochs == [7, 7]
    assert boundary.resolved_epochs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "error"])
@pytest.mark.parametrize("change", ["new_epoch", "blocked", "unchanged"])
async def test_public_graph_failed_embedding_keeps_only_authorized_vector_fallback(recall_boundary, failure, change):
    """Graph provider failure cannot bypass exact-epoch checks on saved vector facts."""
    boundary = recall_boundary
    boundary.pause_embedding_number = 2
    boundary.graph_embedding_failure = failure

    async def recall():
        result = await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
        return result, memory.get_current_retrieved_edge_ids(42)

    task = asyncio.create_task(recall())
    try:
        await boundary.embedding_started.wait()
        assert boundary.private_reads == ["memories"]
        if change == "new_epoch":
            boundary.epoch = 9
        elif change == "blocked":
            boundary.blocked = True
        boundary.unblock()
        result, attribution = await task
        if change == "unchanged":
            assert result == (
                [
                    {
                        "id": 731,
                        "content": "Synthetic candidate fact",
                        "similarity": 0.9,
                        "source_type": "user_message",
                        "created_at": datetime(2026, 10, 9, tzinfo=UTC),
                    }
                ],
                [],
                {},
            )
        else:
            assert result == ([], [], {})
        assert attribution == []
        assert boundary.private_reads == ["memories"]
        assert len(boundary.embeddings) == 2
        assert boundary.acquired_epochs == [7, 7]
        assert boundary.resolved_epochs == []
        assert boundary.judge_prompts == []
        assert boundary.reservations == []
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
async def test_memory_tool_cannot_serialize_recall_from_a_superseded_generation(recall_boundary):
    boundary = recall_boundary
    boundary.pause_expansion = True

    async def recall():
        result = await memory_tools.execute_memory_tool(
            42, "Tell me about my project", "synthetic-key", expected_epoch=7
        )
        return result, memory.get_current_retrieved_edge_ids(42)

    task = asyncio.create_task(recall())
    try:
        await boundary.expansion_started.wait()
        boundary.epoch = 9
        boundary.unblock()
        result, attribution = await task
        assert result == {"memories": [], "graph_triples": [], "total_found": 0}
        assert attribution == []
        assert boundary.embeddings == []
        assert boundary.private_reads == []
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_epoch", "blocked"])
async def test_public_judge_revalidates_candidates_before_quota_or_provider_use(recall_boundary, change):
    """A boolean-only candidate gate sends stored facts to the relevance model."""
    boundary = recall_boundary
    boundary.pause_candidates = True
    task = asyncio.create_task(
        memory.search_memories_with_llm_judge(42, "safe query", "synthetic-key", expected_epoch=7)
    )
    try:
        await boundary.candidates_ready.wait()
        if change == "new_epoch":
            boundary.epoch = 9
        else:
            boundary.blocked = True
        boundary.unblock()
        assert await task == []
        assert boundary.judge_prompts == []
        assert boundary.reservations == []
        assert boundary.acquired_epochs == [7, 7]
        assert boundary.resolved_epochs == []
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
async def test_public_judge_returns_a_same_generation_relevant_fact(recall_boundary):
    boundary = recall_boundary
    result = await memory.search_memories_with_llm_judge(42, "safe query", "synthetic-key", expected_epoch=7)
    assert len(result) == 1
    assert result[0]["id"] == 731
    assert result[0]["llm_judged"] is True
    assert len(boundary.judge_prompts) == 1
    assert "Synthetic candidate fact" in boundary.judge_prompts[0]
    assert len(boundary.reservations) == 1
    assert boundary.acquired_epochs == [7, 7]
    assert boundary.resolved_epochs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("recall", ["vector", "graph", "judge"])
async def test_public_recall_resolves_an_omitted_epoch_once(recall_boundary, recall):
    """Nested phases must inherit the wrapper's one resolved authorization token."""
    boundary = recall_boundary
    if recall == "vector":
        result = await memory.search_memories(42, "safe query", "synthetic-key")
        expected_leases = [7]
    elif recall == "graph":
        result, triples, passages = await memory.search_memories_with_graph(42, "safe query", "synthetic-key")
        assert triples == ["Example — USES → Python ★"]
        assert passages == {"Example — USES → Python": "Synthetic source passage"}
        expected_leases = [7, 7]
    else:
        result = await memory.search_memories_with_llm_judge(42, "safe query", "synthetic-key")
        assert result[0]["llm_judged"] is True
        expected_leases = [7, 7]
    assert [row["id"] for row in result] == [731]
    assert boundary.resolved_epochs == [7]
    assert boundary.acquired_epochs == expected_leases

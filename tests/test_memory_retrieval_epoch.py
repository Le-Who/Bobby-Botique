"""Public recall keeps one durable consent generation through every phase."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.repos import memory, memory_consent, memory_tools
from app.repos.memory import _get_embedding as _provider_get_embedding


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
        self.query_preparations = []
        self.preparation_started = asyncio.Event()
        self.resume_preparation = asyncio.Event()
        self.pause_preparation_number = None
        self.change_graph_model = False
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
        return [0.1, 0.2] + [0.0] * (memory.EMBEDDING_DIMENSION - 2)

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
        self.resume_preparation.set()
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
    original_prepare = memory._get_query_embedding

    async def prepare(user_id, epoch, query, key):
        boundary.query_preparations.append((user_id, epoch, query))
        if len(boundary.query_preparations) == 2 and boundary.change_graph_model:
            monkeypatch.setattr(memory, "EMBEDDING_MODEL", "synthetic-other-embedding-model")
        if len(boundary.query_preparations) == boundary.pause_preparation_number:
            boundary.preparation_started.set()
            await boundary.resume_preparation.wait()
        return await original_prepare(user_id, epoch, query, key)

    monkeypatch.setattr(memory, "_get_query_embedding", prepare)
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
@pytest.mark.parametrize("phase", ["expansion", "vector_embedding", "candidate_retrieval", "graph_preparation"])
async def test_public_graph_keeps_epoch_across_suspended_phases(recall_boundary, phase):
    """Rebinding nested recall or removing graph revalidation exposes a stale graph."""
    boundary = recall_boundary
    boundary.pause_expansion = phase == "expansion"
    boundary.pause_embedding_number = 1 if phase == "vector_embedding" else None
    boundary.pause_preparation_number = 2 if phase == "graph_preparation" else None
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
            "graph_preparation": boundary.preparation_started,
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
            assert len(boundary.embeddings) == 1
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
    assert boundary.embeddings == [("safe query", "RETRIEVAL_QUERY")]


@pytest.mark.asyncio
async def test_public_graph_prepares_a_fresh_embedding_for_each_recall(recall_boundary):
    boundary = recall_boundary
    results = [
        await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7) for _ in range(2)
    ]
    assert results[0] == results[1]
    assert boundary.embeddings == [("safe query", "RETRIEVAL_QUERY")] * 2
    assert boundary.leases == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("rotate_key", [False, True])
async def test_graph_reuses_one_successful_sdk_result_after_original_key_retry(
    recall_boundary, monkeypatch, rotate_key
):
    """Exercise the original provider, including its invalid-key rotation."""
    from unittest.mock import AsyncMock

    from app.handlers import ai_core

    calls = []

    async def embed_content(**kwargs):
        calls.append(kwargs)
        if rotate_key and len(calls) == 1:
            raise RuntimeError("400 invalid api_key synthetic")
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * memory.EMBEDDING_DIMENSION)])

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed_content)))
    resolver = AsyncMock(return_value=({"api_key": "synthetic-rotated-key"}, None, None))
    monkeypatch.setattr(ai_core, "_resolve_ai_request", resolver)
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _key: client)
    monkeypatch.setattr(memory, "_get_embedding", _provider_get_embedding)
    memories, triples, passages = await memory.search_memories_with_graph(
        42, "safe query", "synthetic-key", expected_epoch=7
    )
    assert [row["id"] for row in memories] == [731]
    assert triples == ["Example — USES → Python ★"]
    assert passages == {"Example — USES → Python": "Synthetic source passage"}
    assert len(calls) == (2 if rotate_key else 1)
    assert all(call["config"].task_type == "RETRIEVAL_QUERY" for call in calls)
    assert resolver.await_count == int(rotate_key)


@pytest.mark.asyncio
async def test_original_sdk_failure_does_not_prevent_a_graph_retry(recall_boundary, monkeypatch):
    from unittest.mock import AsyncMock

    from app.metrics import metrics_collector

    calls = []

    async def embed_content(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("Synthetic provider unavailable")
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * memory.EMBEDDING_DIMENSION)])

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed_content)))
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _key: client)
    monkeypatch.setattr(memory, "_get_embedding", _provider_get_embedding)
    monkeypatch.setattr(metrics_collector, "record_error", AsyncMock())
    memories, triples, passages = await memory.search_memories_with_graph(
        42, "safe query", "synthetic-key", expected_epoch=7
    )
    assert memories == []
    assert triples == ["Example — USES → Python ★"]
    assert passages == {"Example — USES → Python": "Synthetic source passage"}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_graph_shares_the_expanded_query_embedding(recall_boundary):
    await memory.search_memories_with_graph(42, "Tell me about my project", "synthetic-key", expected_epoch=7)
    assert recall_boundary.expansions == ["Tell me about my project"]
    assert recall_boundary.embeddings == [("expanded synthetic query", "RETRIEVAL_QUERY")]


@pytest.mark.asyncio
async def test_configuration_change_during_embedding_cannot_label_an_old_result_as_current(
    recall_boundary, monkeypatch
):
    models = []
    initial_model = memory.EMBEDDING_MODEL

    async def embed(*_args, **_kwargs):
        models.append(memory.EMBEDDING_MODEL)
        if len(models) == 1:
            monkeypatch.setattr(memory, "EMBEDDING_MODEL", "synthetic-new-model")
        return [0.1] * memory.EMBEDDING_DIMENSION

    monkeypatch.setattr(memory, "_get_embedding", embed)
    _, triples, _ = await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
    assert models == [initial_model, "synthetic-new-model"]
    assert triples == ["Example — USES → Python ★"]


@pytest.mark.asyncio
async def test_standalone_vector_reads_do_not_keep_an_embedding_between_calls(recall_boundary):
    first = await memory.search_memories(42, "safe query", "synthetic-key", expected_epoch=7)
    second = await memory.search_memories(42, "safe query", "synthetic-key", expected_epoch=7)
    assert first == second
    assert len(recall_boundary.embeddings) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, [], [0.1], "nan", "inf", "zero"])
async def test_graph_retries_an_unsuccessful_or_invalid_vector(recall_boundary, monkeypatch, invalid):
    calls = []

    async def embed(*_args, **_kwargs):
        calls.append(True)
        if len(calls) > 1:
            return [0.1] * memory.EMBEDDING_DIMENSION
        if invalid in ("nan", "inf"):
            return [float(invalid)] * memory.EMBEDDING_DIMENSION
        if invalid == "zero":
            return [0.0] * memory.EMBEDDING_DIMENSION
        return invalid

    monkeypatch.setattr(memory, "_get_embedding", embed)
    _, triples, passages = await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
    assert len(calls) == 2
    assert triples == ["Example — USES → Python ★"]
    assert passages == {"Example — USES → Python": "Synthetic source passage"}


@pytest.mark.asyncio
async def test_concurrent_graph_recalls_do_not_share_embeddings(recall_boundary, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def embed(query, _key, *, task_type):
        calls.append((query, task_type))
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return [0.1] * memory.EMBEDDING_DIMENSION

    monkeypatch.setattr(memory, "_get_embedding", embed)
    tasks = [
        asyncio.create_task(memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7))
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        first, second = await asyncio.gather(*tasks)
        assert first == second
        assert calls == [("safe query", "RETRIEVAL_QUERY")] * 2
        assert recall_boundary.leases == {}
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["query", "key", "model", "dimensions", "user", "epoch"])
async def test_query_reuse_identity_cannot_cross_inputs_or_tenants(recall_boundary, monkeypatch, changed):
    calls = []

    async def embed(query, key, *, task_type):
        calls.append((query, key, task_type))
        return [0.1] * memory.EMBEDDING_DIMENSION

    async def recall(user_id, query, key, *, expected_epoch, **_kwargs):
        await memory._get_query_embedding(user_id, expected_epoch, query, key)
        if changed == "model":
            monkeypatch.setattr(memory, "EMBEDDING_MODEL", "synthetic-other-model")
        elif changed == "dimensions":
            monkeypatch.setattr(memory, "EMBEDDING_DIMENSION", 3)
        await memory._get_query_embedding(
            user_id + int(changed == "user"),
            expected_epoch + int(changed == "epoch"),
            query + (" other" if changed == "query" else ""),
            key + (" other" if changed == "key" else ""),
        )
        return [], [], {}

    monkeypatch.setattr(memory, "_get_embedding", embed)
    monkeypatch.setattr(memory, "_search_memories_with_graph_impl", recall)
    await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
    assert len(calls) == 2
    assert memory._query_embedding_reuse.get() is None


@pytest.mark.asyncio
async def test_query_reuse_returns_a_copy_and_retires_its_private_holder(recall_boundary, monkeypatch):
    holders = []

    async def recall(user_id, query, key, *, expected_epoch, **_kwargs):
        first = await memory._get_query_embedding(user_id, expected_epoch, query, key)
        holders.append(memory._query_embedding_reuse.get())
        first[0] = 99.0
        second = await memory._get_query_embedding(user_id, expected_epoch, query, key)
        assert second[0] == 0.1
        return [], [], {}

    monkeypatch.setattr(memory, "_search_memories_with_graph_impl", recall)
    await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
    assert len(recall_boundary.embeddings) == 1
    assert memory._query_embedding_reuse.get() is None
    assert holders[0].identity is None and holders[0].vector is None and holders[0].owner is None


@pytest.mark.asyncio
async def test_nested_graph_recall_restores_the_outer_query_scope(recall_boundary, monkeypatch):
    async def recall(user_id, query, key, *, expected_epoch, **_kwargs):
        await memory._get_query_embedding(user_id, expected_epoch, query, key)
        if user_id == 42:
            await memory.search_memories_with_graph(43, "nested query", key, expected_epoch=expected_epoch)
            await memory._get_query_embedding(user_id, expected_epoch, query, key)
        return [], [], {}

    monkeypatch.setattr(memory, "_search_memories_with_graph_impl", recall)
    await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
    assert recall_boundary.embeddings == [("safe query", "RETRIEVAL_QUERY"), ("nested query", "RETRIEVAL_QUERY")]
    assert recall_boundary.acquired_epochs == [7, 7]
    assert memory._query_embedding_reuse.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("after_exit", [False, True])
async def test_inherited_child_context_cannot_reuse_or_retain_the_owner_vector(
    recall_boundary, monkeypatch, after_exit
):
    release = asyncio.Event()
    children = []
    inherited = []

    async def recall(user_id, query, key, *, expected_epoch, **_kwargs):
        await memory._get_query_embedding(user_id, expected_epoch, query, key)

        async def child():
            inherited.append(memory._query_embedding_reuse.get())
            await release.wait()
            return await memory._get_query_embedding(user_id, expected_epoch, query, key)

        children.append(asyncio.create_task(child()))
        await asyncio.sleep(0)
        if not after_exit:
            release.set()
            await children[0]
            await memory._get_query_embedding(user_id, expected_epoch, query, key)
        return [], [], {}

    monkeypatch.setattr(memory, "_search_memories_with_graph_impl", recall)
    try:
        await memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7)
        assert inherited[0].identity is None and inherited[0].vector is None and inherited[0].owner is None
        release.set()
        assert len(await asyncio.gather(*children)) == 1
        assert len(recall_boundary.embeddings) == 2
    finally:
        release.set()
        await asyncio.gather(*children, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["embedding", "reuse"])
async def test_cancelled_graph_retires_query_vector_and_releases_leases(recall_boundary, monkeypatch, phase):
    boundary = recall_boundary
    boundary.pause_embedding_number = 1 if phase == "embedding" else None
    boundary.pause_preparation_number = 2 if phase == "reuse" else None
    prepare = memory._get_query_embedding
    holders = []

    async def observed_prepare(*args, **kwargs):
        holders.append(memory._query_embedding_reuse.get())
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(memory, "_get_query_embedding", observed_prepare)
    task = asyncio.create_task(memory.search_memories_with_graph(42, "safe query", "synthetic-key", expected_epoch=7))
    try:
        started = boundary.embedding_started if phase == "embedding" else boundary.preparation_started
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert boundary.leases == {}
        assert memory._query_embedding_reuse.get() is None
        assert all(holder.identity is None and holder.vector is None and holder.owner is None for holder in holders)
    finally:
        await _finish_task(task, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "error"])
@pytest.mark.parametrize("change", ["new_epoch", "blocked", "unchanged"])
async def test_public_graph_failed_retry_keeps_only_authorized_vector_fallback(recall_boundary, failure, change):
    """A changed model requires a retry, whose failure cannot bypass epoch checks."""
    boundary = recall_boundary
    boundary.change_graph_model = True
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

"""Legacy direct Gemini attempts must reserve RPD before external work."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CALLERS = ("expand", "relevance", "extract", "taxonomy", "consolidate", "brief", "brief_lease")


@pytest.fixture
def baseline_call(monkeypatch):
    from app.handlers import scheduled_briefs as briefs
    from app.repos import memory
    from app.repos import memory_consolidation as consolidation
    from app.repos import memory_extraction as extraction

    events = []
    responses = []
    reservation_results = []
    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())

    async def reserve(key_hash, model):
        events.append(("reserve", key_hash, model))
        result = reservation_results.pop(0) if reservation_results else True
        if isinstance(result, Exception):
            raise result
        return result

    async def generate(**kwargs):
        events.append(("sdk", kwargs["model"]))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "test-key", "key_hash": hashlib.sha256(b"test-key").hexdigest()}),
    )
    monkeypatch.setattr(memory, "search_memories", AsyncMock(return_value=[{"content": "Alice codes", "id": 17}]))
    monkeypatch.setattr(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(briefs, "_is_ltm_snapshot_current", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.handlers.ai_core._resolve_ai_request",
        AsyncMock(
            return_value=({"api_key": "test-key", "key_hash": hashlib.sha256(b"test-key").hexdigest()}, None, None)
        ),
    )

    @asynccontextmanager
    async def lease(*args, **kwargs):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    for module in (memory, extraction, consolidation, briefs):
        monkeypatch.setattr(module, "run_gemini_override", AsyncMock(return_value=None))

    async def invoke(name):
        if name == "expand":
            return await memory.expand_query_with_llm("coding question", "test-key")
        if name == "relevance":
            return await memory._search_memories_with_llm_judge_impl(42, "coding question", "test-key")
        if name == "extract":
            return await extraction.extract_graph_structured("Alice codes", "test-key")
        if name == "taxonomy":
            return await extraction._resolve_ambiguous_conflict(
                "likes Python", "likes Rust", "Alice", "code", "test-key"
            )
        if name == "consolidate":
            return await consolidation._extract_graph("memory_id=17: Alice codes", "test-key")
        return await briefs._generate_brief_summary(
            ["coding"], [], user_id=42 if name == "brief_lease" else None, expected_epoch=0
        )

    return SimpleNamespace(
        invoke=invoke,
        events=events,
        responses=responses,
        reservation_results=reservation_results,
        health=health,
        modules=(memory, extraction, consolidation, briefs),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("refusal", [False, ConnectionError("quota store unavailable")])
async def test_baseline_reservation_refusal_never_sends_or_penalizes_key(baseline_call, caller, refusal):
    case = baseline_call
    case.reservation_results.append(refusal)
    case.responses.extend(["invalid"] * 3)
    await case.invoke(caller)
    assert [event[0] for event in case.events] == ["reserve"]
    case.health.suspend_key.assert_not_awaited()
    case.health.record_success.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "response"),
    [
        ("expand", "Python project"),
        ("relevance", '[{"index":0,"relevant":true}]'),
        ("extract", '{"entities":[],"relations":[]}'),
        ("taxonomy", "update"),
        ("consolidate", '{"facts":[],"entities":[],"relations":[]}'),
        ("brief", '{"coding":"News summary"}'),
        ("brief_lease", '{"coding":"News summary"}'),
    ],
)
async def test_baseline_success_reserves_selected_key_and_model_once_before_sdk(baseline_call, caller, response):
    case = baseline_call
    case.responses.append(response)
    result = await case.invoke(caller)
    assert result is not None
    assert [event[0] for event in case.events] == ["reserve", "sdk"]
    assert case.events[0][1] == hashlib.sha256(b"test-key").hexdigest()
    assert case.events[0][2] == case.events[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, ConnectionError("quota store unavailable")])
async def test_consolidation_parse_fallback_requires_its_own_reservation(baseline_call, refusal):
    case = baseline_call
    case.responses.append("invalid JSON")
    case.reservation_results.extend([True, refusal])
    result = await case.invoke("consolidate")
    assert result == {"facts": [], "entities": [], "relations": []}
    assert [event[0] for event in case.events] == ["reserve", "sdk", "reserve"]
    case.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_consolidation_successful_parse_fallback_counts_both_attempts(baseline_call):
    case = baseline_call
    case.responses.extend(["invalid JSON", "- Alice codes"])
    result = await case.invoke("consolidate")
    assert result["facts"] == ["Alice codes"]
    assert [event[0] for event in case.events] == ["reserve", "sdk", "reserve", "sdk"]


@pytest.mark.asyncio
async def test_extraction_retry_reserves_again_before_second_sdk_attempt(baseline_call):
    case = baseline_call
    case.responses.extend(['{"entities":[', '{"entities":[],"relations":[]}'])
    result = await case.invoke("extract")
    assert result.entities == []
    assert [event[0] for event in case.events] == ["reserve", "sdk", "reserve", "sdk"]
    case.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, ConnectionError("quota store unavailable")])
async def test_extraction_retry_quota_failure_does_not_penalize_key(baseline_call, refusal):
    case = baseline_call
    case.responses.append('{"entities":[')
    case.reservation_results.extend([True, refusal])
    result = await case.invoke("extract")
    assert result.entities == []
    assert [event[0] for event in case.events] == ["reserve", "sdk", "reserve"]
    case.health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_override_callbacks_do_not_reserve_a_second_time(baseline_call, monkeypatch, caller):
    case = baseline_call
    case.responses.append(
        {
            "expand": "Python project",
            "relevance": "[]",
            "extract": '{"entities":[],"relations":[]}',
            "taxonomy": "parallel",
            "consolidate": '{"facts":[],"entities":[],"relations":[]}',
        }.get(caller, '{"coding":"News summary"}')
    )

    async def reserved_override(process_id, baseline, execute, **kwargs):
        # The real helper owns this reservation; simulate that ownership boundary.
        case.events.append(("helper_reserve",))
        return await execute("gemini-3.8-flash", "test-key")

    for module in case.modules:
        monkeypatch.setattr(module, "run_gemini_override", reserved_override)
    await case.invoke(caller)
    assert [event[0] for event in case.events] == ["helper_reserve", "sdk"]


@pytest.mark.asyncio
async def test_expansion_quota_store_wait_respects_optional_deadline(baseline_call, monkeypatch):
    case = baseline_call
    cancelled = asyncio.Event()

    async def slow_reserve(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", slow_reserve)
    monkeypatch.setattr(case.modules[0], "QUERY_EXPANSION_TIMEOUT_SECONDS", 0.01)
    assert await asyncio.wait_for(case.invoke("expand"), 0.25) == "coding question"
    assert cancelled.is_set()
    assert case.events == []


@pytest.mark.asyncio
async def test_expansion_reservation_and_sdk_share_one_deadline(baseline_call, monkeypatch):
    case = baseline_call
    cancelled = asyncio.Event()

    async def slow_reserve(*args):
        await asyncio.sleep(0.25)
        return True

    async def slow_generate(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=slow_generate)))
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", slow_reserve)
    monkeypatch.setattr(case.modules[0], "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(case.modules[0], "QUERY_EXPANSION_TIMEOUT_SECONDS", 0.4)
    assert await asyncio.wait_for(case.invoke("expand"), 0.53) == "coding question"
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [False, ConnectionError("quota store unavailable")])
async def test_override_consolidation_fallback_quota_failure_does_not_suspend_key(baseline_call, monkeypatch, refusal):
    from app.runtime_settings import gemini_execution

    case = baseline_call
    case.responses.append("invalid JSON")
    case.reservation_results.extend([True, refusal])
    monkeypatch.setattr(case.modules[2], "run_gemini_override", gemini_execution.run_gemini_override)
    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=SimpleNamespace(explicit=True, models=("gemini-3.8-flash",))),
    )
    result = await case.invoke("consolidate")
    assert result == {"facts": [], "entities": [], "relations": []}
    assert [event[0] for event in case.events] == ["reserve", "sdk", "reserve"]
    case.health.suspend_key.assert_not_awaited()
    case.health.record_success.assert_not_awaited()

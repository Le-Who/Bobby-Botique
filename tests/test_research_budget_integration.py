"""Exercise cancellation and accounting through the real research loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.genai import types

from app.core import agentic as agentic_module
from app.core.agentic import AgenticSearch
from app.core.research_budget import ResearchBudget


def response(text=None, call=None, tokens=10):
    part = types.Part.from_text(text=text) if text else types.Part(function_call=call)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=tokens)
        if tokens is not None
        else None,
    )


def agent_with(generate, budget):
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    with patch("app.core.agentic.get_cached_genai_client", return_value=client):
        return AgenticSearch("test-model", "fake-key", budget=budget)


@pytest.fixture
def observed_timeouts(monkeypatch):
    timeouts = []
    delays = []

    def observe_timeout(delay):
        timeout = asyncio.timeout(delay)
        timeouts.append(timeout)
        delays.append(delay)
        return timeout

    monkeypatch.setattr(agentic_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "timeout": observe_timeout}))
    return timeouts, delays


async def test_hanging_iteration_cancelled_then_synthesis_uses_reserve():
    cancelled = asyncio.Event()

    async def generate(**kwargs):
        if kwargs["config"].tools:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return response(text="Synthesis from available context")

    budget = ResearchBudget(timeout_seconds=1.2)
    result = await agent_with(generate, budget).run("query", AsyncMock())
    assert cancelled.is_set()
    assert result.answer == "Synthesis from available context"
    assert result.budget_reason == "deadline"
    assert result.usage_uncertain


@pytest.mark.parametrize("expired_scope", ["synthesis", "outer"])
async def test_hanging_synthesis_is_cancelled_at_total_deadline(observed_timeouts, expired_scope):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    timeouts, delays = observed_timeouts
    now = [asyncio.get_running_loop().time()]

    async def generate(**kwargs):
        if kwargs["config"].tools:
            return types.GenerateContentResponse(
                candidates=[], usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=10)
            )
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    budget = ResearchBudget(timeout_seconds=30, clock=lambda: now[0])
    task = asyncio.create_task(agent_with(generate, budget).run("query", AsyncMock()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert delays == [30, 25, 30]  # Whole request, iteration, then synthesis reserve.
        # Either real timeout may deliver cancellation first at the shared deadline.
        # Expire it after SDK entry so cold setup and scheduler speed cannot choose the branch.
        now[0] += budget.timeout_seconds
        timeouts[2 if expired_scope == "synthesis" else 0].reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(task, 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()
    assert timeouts[0].expired() == (expired_scope == "outer")
    assert timeouts[2].expired() == (expired_scope == "synthesis")
    assert budget.remaining_seconds() == 0
    assert result.answer.startswith("❌")
    assert result.budget_reason == "deadline"
    assert result.usage_uncertain
    assert result.total_tokens == budget.tokens_known == 10
    assert budget.unknown_usage_calls == 1


@pytest.mark.parametrize("stage", ["iteration", "synthesis"])
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
async def test_provider_failure_does_not_exhaust_time_budget(observed_timeouts, caplog, stage, error_type):
    timeouts, delays = observed_timeouts
    calls = []

    async def generate(**kwargs):
        current_stage = "iteration" if kwargs["config"].tools else "synthesis"
        calls.append(current_stage)
        if current_stage == stage:
            raise error_type("offline provider failure")
        if current_stage == "iteration":
            return types.GenerateContentResponse(
                candidates=[], usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=10)
            )
        return response(text="Synthesis from available context")

    budget = ResearchBudget(timeout_seconds=30, clock=lambda: 100.0)
    result = await asyncio.wait_for(agent_with(generate, budget).run("query", AsyncMock()), 5)
    if stage == "iteration" and error_type is RuntimeError:
        assert calls == ["iteration"]
        assert delays == [30, 25]
        expected_tokens = 0
    else:
        assert calls == ["iteration", "synthesis"]
        assert delays == [30, 25, 30]
        expected_tokens = 10
    if stage == "iteration" and error_type is TimeoutError:
        assert result.answer == "Synthesis from available context"
    else:
        assert result.answer.startswith("❌")
    assert not any(timeout.expired() for timeout in timeouts)
    assert result.budget_reason is budget.reason is None
    assert budget.remaining_seconds() == 30
    assert result.usage_uncertain
    assert result.total_tokens == budget.tokens_known == expected_tokens
    assert budget.unknown_usage_calls == 1
    failures = [
        record for record in caplog.records if getattr(record, "_event_name", None) == "workload.attempt_failed"
    ]
    assert len(failures) == 1
    assert failures[0].reason_code == ("deadline" if error_type is TimeoutError else "provider_error")


async def test_external_cancellation_propagates_and_accounts_unknown_usage():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def generate(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    budget = ResearchBudget()
    task = asyncio.create_task(agent_with(generate, budget).run("query", AsyncMock()))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set()
    assert budget.usage_uncertain
    assert budget.reason is None


async def test_fallback_cannot_reset_pages_or_usage():
    budget = ResearchBudget(max_pages=1)
    read = types.FunctionCall(name="read_page", args={"url": "https://example.com"})
    first = agent_with(AsyncMock(side_effect=[response(call=read, tokens=None), RuntimeError("offline")]), budget)
    second = agent_with(AsyncMock(side_effect=[response(call=read), response(text="Answer")]), budget)
    executed = []

    async def tool(*args, **kwargs):
        executed.append(args[0].name)
        return {"content": "Fake page"}

    with (
        patch.object(first, "_execute_tool", side_effect=tool),
        patch.object(second, "_execute_tool", side_effect=tool),
    ):
        await first.run("query", AsyncMock())
        result = await second.run("query", AsyncMock())
    assert result.answer == "Answer"
    assert executed == ["read_page"]
    assert budget.attempts_used == 2
    assert budget.pages_used == 1
    assert budget.unknown_usage_calls == 2
    assert budget.tokens_known == 20


async def test_tool_batch_cancelled_before_synthesis():
    stopped = asyncio.Event()
    search = types.FunctionCall(name="search_web", args={"queries": ["test"]})
    budget = ResearchBudget(timeout_seconds=0.3)
    agent = agent_with(AsyncMock(side_effect=[response(call=search), response(text="Limited answer")]), budget)

    async def tool(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with patch.object(agent, "_execute_tool", side_effect=tool):
        result = await agent.run("query", AsyncMock())
    assert stopped.is_set()
    assert result.answer == "Limited answer"
    assert result.budget_reason == "deadline"


async def test_synthesis_keeps_fast_tool_result_when_sibling_times_out():
    captured = []
    calls = [types.FunctionCall(name="read_page", args={"url": url}) for url in ["fast", "slow"]]
    first = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(function_call=call) for call in calls])
            )
        ]
    )

    async def generate(**kwargs):
        if kwargs["config"].tools:
            return first
        for content in kwargs["contents"]:
            for part in content.parts or []:
                if part.function_response:
                    captured.append(part.function_response.response)
        return response(text="Answer with preserved evidence")

    async def tool(call, *args, **kwargs):
        if call.args["url"] == "fast":
            return {"content": "Completed evidence"}
        await asyncio.Event().wait()

    agent = agent_with(generate, ResearchBudget(timeout_seconds=1.2))
    with patch.object(agent, "_execute_tool", side_effect=tool):
        await agent.run("query", AsyncMock())
    assert {"content": "Completed evidence"} in captured
    assert sum("error" in row for row in captured) == 1

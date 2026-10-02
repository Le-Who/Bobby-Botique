"""Exercise cancellation and accounting through the real research loop."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.genai import types

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


async def test_hanging_synthesis_is_cancelled_at_total_deadline():
    cancelled = asyncio.Event()

    async def generate(**kwargs):
        if kwargs["config"].tools:
            return types.GenerateContentResponse(candidates=[])
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    budget = ResearchBudget(timeout_seconds=0.15)
    result = await agent_with(generate, budget).run("query", AsyncMock())
    assert cancelled.is_set()
    assert result.answer.startswith("❌")
    assert result.budget_reason == "deadline"


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
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert budget.usage_uncertain


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

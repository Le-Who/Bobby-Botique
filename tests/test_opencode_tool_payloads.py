import asyncio
import json

import httpx
import pytest

from app.providers import openrouter
from app.providers.opencode import OpencodeGoProvider


@pytest.mark.asyncio
async def test_opencode_chat_payload_options_belong_to_each_request(monkeypatch):
    calls = {}
    entered = asyncio.Event()
    release = asyncio.Event()

    async def post(request):
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        calls[prompt] = body
        if prompt == "fails":
            raise httpx.ConnectError("Synthetic connection failure", request=request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpencodeGoProvider("offline-test-key")
    build_messages = provider._build_messages

    async def build(history, system_instruction):
        if history[-1]["parts"] == ["overlapped"]:
            entered.set()
            await release.wait()
        return await build_messages(history, system_instruction)

    monkeypatch.setattr(provider, "_build_messages", build)

    tools = [{"type": "function", "function": {"name": "lookup"}}]
    choice = {"type": "function", "function": {"name": "lookup"}}

    async def request(prompt, *, model="opencode-go/deepseek-v4-pro", selected_tools=None, tool_choice=None):
        return await provider._execute_request(
            history=[{"role": "user", "parts": [prompt]}],
            model_name=model,
            system_instruction=None,
            user_id=None,
            chat_id=None,
            timeout=1,
            thinking_level="high",
            tools=selected_tools,
            tool_choice=tool_choice,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(post)) as client:
        monkeypatch.setattr(openrouter, "_openrouter_http_client", client)

        assert (await request("first", selected_tools=tools, tool_choice=choice)).success
        assert (await request("plain", tool_choice=choice)).success
        assert not (await request("fails", selected_tools=tools, tool_choice="auto")).success
        assert (await request("after failure")).success

        pending = asyncio.create_task(request("overlapped", selected_tools=tools, tool_choice=choice))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert (await request("concurrent plain")).success
        finally:
            release.set()
            assert (await pending).success

        assert (await request("unsupported", model="opencode-go/unlisted-model", selected_tools=tools)).success
        assert (await request("messages", model="opencode-go/minimax-m2.5", selected_tools=tools)).success

    for prompt in ("first", "overlapped"):
        assert calls[prompt]["tools"] == tools
        assert calls[prompt]["tool_choice"] == choice
        assert calls[prompt]["reasoning_effort"] == "high"

    assert calls["fails"]["tool_choice"] == "auto"
    for prompt in ("plain", "after failure", "concurrent plain", "unsupported", "messages"):
        assert "tools" not in calls[prompt]
        assert "tool_choice" not in calls[prompt]

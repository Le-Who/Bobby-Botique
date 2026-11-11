"""Game races retain provider finalizers until their request owner exits."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.games import judge, word_bank


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", ["word", "hints"])
@pytest.mark.parametrize("finish", ["winner", "repeated_cancel"])
async def test_game_generation_awaits_provider_cleanup(monkeypatch, generation, finish):
    provider_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_interrupted = False

    async def vertex(**kwargs):
        nonlocal cleanup_interrupted
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            try:
                await cleanup_release.wait()
            except asyncio.CancelledError:
                cleanup_interrupted = True
                raise
            finally:
                cleanup_finished.set()

    async def router_response(*, preferred_model, **kwargs):
        if not preferred_model.startswith("opencode-go/"):
            return "", None
        await provider_entered.wait()
        if finish == "repeated_cancel":
            await asyncio.Event().wait()
        return ('{"hints":["первый","второй","третий"]}' if generation == "hints" else "шериф"), None

    monkeypatch.setattr("app.config.settings.VERTEX_AI_KEY", "synthetic-vertex-key")
    monkeypatch.setattr("app.config.settings.OPENCODE_AVAILABLE_MODELS", ["opencode-go/glm-5.1"])
    monkeypatch.setattr("app.config.settings.OPENCODE_INLINE_MODEL", "opencode-go/glm-5.1")
    monkeypatch.setattr(
        "app.providers.gemini.get_vertex_client",
        lambda: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=vertex))),
    )
    router = SimpleNamespace(get_response=router_response)
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)
    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: router)
    for module in (judge, word_bank):
        monkeypatch.setattr(module, "acquire_foreground_slot", AsyncMock(side_effect=lambda *args: nullcontext()))
        monkeypatch.setattr(module, "record_result", AsyncMock())

    work = (
        word_bank._generate_single_word_fast("персонаж", model="")
        if generation == "word"
        else judge.generate_hints("шериф", "персонаж", model="")
    )
    owner = asyncio.create_task(work)
    try:
        await asyncio.wait_for(provider_entered.wait(), 2)
        if finish == "repeated_cancel":
            owner.cancel("first owner cancellation")
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        await asyncio.sleep(0)
        assert not owner.done(), "The game request exited before its provider finished cleanup"
        if finish == "repeated_cancel":
            owner.cancel("second owner cancellation")
            await asyncio.sleep(0)
            assert not owner.done()
            assert not cleanup_interrupted
        cleanup_release.set()
        if finish == "repeated_cancel":
            with pytest.raises(asyncio.CancelledError):
                await owner
        else:
            assert await owner == (["первый", "второй", "третий"] if generation == "hints" else "шериф")
        assert cleanup_finished.is_set()
        assert not cleanup_interrupted
    finally:
        cleanup_release.set()
        if not owner.done() and not owner.cancelling():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.wait_for(cleanup_finished.wait(), 2)

"""Detached workers must obtain a fresh settings snapshot for each operation."""

import asyncio
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest

from app.observability.context import current_context, request_scope
from app.prompt_registry import get_prompt_text
from app.runtime_settings import lifecycle, store
from app.utils.background_tasks import TaskManager, start_background_task


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked", [False, True])
async def test_detached_worker_refreshes_each_job_without_losing_trace(monkeypatch, tracked):
    first = store.SettingsSnapshot(7, MappingProxyType({"prompt:formatting_rules": "first"}))
    second = store.SettingsSnapshot(8, MappingProxyType({"prompt:formatting_rules": "second"}))
    latest = [first]
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(side_effect=lambda **_: latest[0]))
    refresh = AsyncMock()
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", refresh)
    started = asyncio.Event()
    release = asyncio.Event()
    rows = []

    async def worker():
        async with lifecycle.runtime_settings_scope():
            rows.append(((await lifecycle.operation_snapshot()).revision, get_prompt_text("formatting_rules")))
            started.set()
            await release.wait()
            # The current job remains pinned even after an admin edit.
            rows.append(((await lifecycle.operation_snapshot()).revision, get_prompt_text("formatting_rules")))
        async with lifecycle.runtime_settings_scope():
            rows.append(((await lifecycle.operation_snapshot()).revision, get_prompt_text("formatting_rules")))
        assert current_context().trace_id == "d" * 32

    manager = TaskManager()
    with request_scope(request_id="d" * 32, user_id=42):
        async with lifecycle.runtime_settings_scope(first):
            task = manager.submit(worker()) if tracked else start_background_task(None, worker, "fresh-worker")
            try:
                await asyncio.wait_for(started.wait(), 1)
                latest[0] = second
                release.set()
                await asyncio.wait_for(task, 1)
                assert (await lifecycle.operation_snapshot()).revision == 7
                assert get_prompt_text("formatting_rules") == "first"
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert rows == [(7, "first"), (7, "first"), (8, "second")]
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_voice_fifo_second_job_uses_new_revision_while_first_stays_pinned(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from app import voice_engine
    from app.utils import audio

    def snapshot(revision):
        return store.SettingsSnapshot(
            revision,
            MappingProxyType(
                {
                    "process:tts.delivery": {
                        "models": ("gemini",),
                        "strategy": "sequential",
                        "inherit_user_model": False,
                    },
                    "process:tts": {
                        "models": (f"gemini-revision{revision}-tts",),
                        "strategy": "sequential",
                        "inherit_user_model": False,
                    },
                    "prompt:formatting_rules": f"revision{revision}",
                }
            ),
        )

    first, second = snapshot(7), snapshot(8)
    latest = [first]
    monkeypatch.setattr(store, "get_snapshot", AsyncMock(side_effect=lambda **_: latest[0]))
    monkeypatch.setattr(lifecycle, "refresh_runtime_settings", AsyncMock())

    @asynccontextmanager
    async def lease(*_, **__):
        yield True

    monkeypatch.setattr("app.repos.memory_consent.private_data_lease", lease)
    started, release = asyncio.Event(), asyncio.Event()
    rows = []

    async def synthesize(*_, model_name, **__):
        rows.append((model_name, get_prompt_text("formatting_rules")))
        if len(rows) == 1:
            started.set()
            await release.wait()
            rows.append((model_name, get_prompt_text("formatting_rules")))
        return [b"pcm"]

    monkeypatch.setattr(voice_engine, "_run_gemini_pipeline", synthesize)
    monkeypatch.setattr(audio, "crossfade_pcm_chunks", lambda parts: b"pcm")
    monkeypatch.setattr(audio, "pcm_to_ogg_opus", AsyncMock(return_value=b"ogg"))
    manager = voice_engine.VoiceReplyManager()
    manager._send_ogg = AsyncMock()
    manager._set_status = AsyncMock()
    manager._refresh_queued_statuses = AsyncMock()
    bot = MagicMock()
    async with lifecycle.runtime_settings_scope(first):
        try:
            await manager.enqueue(
                bot=bot,
                user_id=42,
                chat_id=42,
                reply_to_message_id=1,
                response_text="first",
                source_key="first",
                expected_epoch=1,
            )
            await asyncio.wait_for(started.wait(), 2)
            latest[0] = second
            await manager.enqueue(
                bot=bot,
                user_id=42,
                chat_id=42,
                reply_to_message_id=2,
                response_text="second",
                source_key="second",
                expected_epoch=1,
            )
            release.set()
            await manager.wait_until_idle(42, timeout=2)
        finally:
            await manager.purge_user_jobs(42)
    assert rows == [
        ("gemini-revision7-tts", "revision7"),
        ("gemini-revision7-tts", "revision7"),
        ("gemini-revision8-tts", "revision8"),
    ]
    assert manager._send_ogg.await_count == 2

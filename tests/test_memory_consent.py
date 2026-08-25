"""Consent and lifecycle invariants for automatic long-term-memory capture."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_capture_epoch_requires_enabled_ltm():
    from app.repos.memory_consent import capture_epoch

    assert capture_epoch(SimpleNamespace(ltm_enabled=False, memory_epoch=7)) is None
    assert capture_epoch(SimpleNamespace(ltm_enabled=True, memory_epoch=7, private_data_blocked=True)) is None
    assert capture_epoch(SimpleNamespace(ltm_enabled=True, memory_epoch=7)) == 7
    assert capture_epoch(SimpleNamespace(ltm_enabled=True)) == 0


def test_generic_chat_save_cannot_overwrite_ltm_consent_from_stale_state():
    from app.repos.chats import update_user_chat

    source = inspect.getsource(update_user_chat)
    conflict_update = source.split("ON CONFLICT (user_id)", 1)[1].split("),\n            update_users", 1)[0]
    assert "ltm_enabled = EXCLUDED.ltm_enabled" not in conflict_update


@pytest.mark.asyncio
async def test_dedicated_ltm_toggle_returns_trigger_managed_epoch():
    from app.repos import chats

    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    wait_for_leases = AsyncMock()
    voice_manager = MagicMock()
    voice_manager.purge_user_jobs = AsyncMock()

    with (
        patch.object(chats, "db_manager", SimpleNamespace(pool=pool, is_connected=True)),
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
        patch.object(chats, "db_query", new_callable=AsyncMock, return_value=[{"memory_epoch": 8}]) as query,
        patch("app.repos.memory_consent.wait_for_private_data_leases", wait_for_leases),
        patch("app.voice_engine.get_voice_reply_manager", return_value=voice_manager),
    ):
        epoch = await chats.set_ltm_enabled(42, False)

    assert epoch == 8
    sql = query.await_args.args[0]
    assert "INSERT INTO public.chats" in sql
    assert "ON CONFLICT (user_id)" in sql
    assert "ltm_enabled = EXCLUDED.ltm_enabled" in sql
    assert "RETURNING memory_epoch" in sql
    assert query.await_args.kwargs["conn"] is conn
    wait_for_leases.assert_awaited_once_with(42, before_epoch=8, ltm_only=True)
    voice_manager.purge_user_jobs.assert_awaited_once_with(42, ltm_only=True)


@pytest.mark.asyncio
async def test_first_chat_insert_refreshes_sequence_allocated_generation():
    from app.database import ChatState
    from app.repos import chats

    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    chat_state = ChatState(
        history=[],
        model="gemini-3.1-flash-lite",
        token_count=0,
        search_enabled=False,
        system_prompt=None,
        memory_epoch=0,
    )

    with (
        patch.object(chats, "db_manager", SimpleNamespace(pool=pool, is_connected=True)),
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
        patch.object(chats, "db_query", new_callable=AsyncMock, return_value=[{"memory_epoch": 44}]),
    ):
        await chats.update_user_chat(42, chat_state)

    assert chat_state.memory_epoch == 44


@pytest.mark.asyncio
async def test_summary_compare_and_swap_is_bound_to_account_generation():
    from app.repos import chats

    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    query = AsyncMock(return_value=[])

    with (
        patch.object(chats, "db_manager", SimpleNamespace(pool=pool, is_connected=True)),
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
        patch.object(chats, "db_query", query),
    ):
        persisted = await chats.replace_context_summary(
            42,
            expected_summary=None,
            new_summary="stale old-account summary",
            expected_epoch=7,
        )

    assert persisted is False
    sql = " ".join(query.await_args.args[0].split())
    assert "memory_epoch = $4" in sql
    assert "private_data_blocked IS FALSE" in sql
    assert query.await_args.args[1] == (42, None, "stale old-account summary", 7)


@pytest.mark.asyncio
async def test_memory_task_is_registered_per_user_and_can_be_cancelled():
    from app.repos import memory_autosave

    started = asyncio.Event()

    async def pending_write() -> None:
        started.set()
        await asyncio.Event().wait()

    task = memory_autosave.submit_memory_task(42, pending_write, retry=0)
    await started.wait()

    assert task in memory_autosave._inflight_memory_tasks
    await memory_autosave.cancel_user_memory_tasks(42)
    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_media_graph_child_is_registered_and_cancelled_with_user_memory_tasks():
    from app.repos import memory_autosave
    from app.utils.multimodal_processor import process_media_for_memory

    user_id = 4242
    started = asyncio.Event()
    stopped = asyncio.Event()
    legacy_tasks: list[asyncio.Task] = []

    async def pending_graph(**_kwargs) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def capture_legacy_task(coro):
        task = asyncio.create_task(coro)
        legacy_tasks.append(task)
        return task

    @asynccontextmanager
    async def allowed_lease(*_args, **_kwargs):
        yield True

    await memory_autosave.cancel_user_memory_tasks(user_id)
    cancelled = 0
    try:
        with (
            patch("app.repos.memory_consent.private_data_lease", allowed_lease),
            patch(
                "app.utils.multimodal_processor._transcribe_voice_for_ltm",
                new_callable=AsyncMock,
                return_value="A sufficiently detailed media-derived memory for graph extraction",
            ),
            patch("app.repos.memory.store_memory", new_callable=AsyncMock, return_value=77),
            patch("app.utils.multimodal_processor._extract_graph_from_media", new=pending_graph),
            patch("app.utils.background_tasks.submit_task", side_effect=capture_legacy_task),
        ):
            result = await process_media_for_memory(
                b"private audio",
                user_id=user_id,
                media_type="voice",
                api_key="key",
                expected_epoch=9,
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            cancelled = await memory_autosave.cancel_user_memory_tasks(user_id)
            await asyncio.sleep(0)
    finally:
        await memory_autosave.cancel_user_memory_tasks(user_id)
        for task in legacy_tasks:
            task.cancel()
        if legacy_tasks:
            await asyncio.gather(*legacy_tasks, return_exceptions=True)

    assert result == 77
    assert cancelled == 1
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_background_capture_passes_schedule_epoch_to_repository():
    from app.handlers.ai_chat import _store_memory_in_background

    submitted_factory = None

    def capture_submit(user_id, factory, *, retry):
        nonlocal submitted_factory
        assert user_id == 42
        submitted_factory = factory
        return asyncio.create_task(asyncio.sleep(0))

    with (
        patch("app.repos.memory_autosave.submit_memory_task", side_effect=capture_submit),
        patch(
            "app.repos.keys.get_available_gemini_key",
            new_callable=AsyncMock,
            return_value={"api_key": "key"},
        ),
        patch("app.repos.memory.store_memory", new_callable=AsyncMock, return_value=None) as store,
    ):
        _store_memory_in_background(42, "A sufficiently long memory capture message", expected_epoch=9)
        assert submitted_factory is not None
        await submitted_factory()

    assert store.await_args.kwargs["expected_epoch"] == 9


@pytest.mark.asyncio
async def test_media_file_metadata_is_bound_to_live_source_and_epoch():
    from app.utils.multimodal_processor import _extract_graph_from_media

    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire

    with (
        patch(
            "app.repos.memory_extraction.extract_and_store_graph",
            new_callable=AsyncMock,
            return_value=1,
        ),
        patch("app.database.db_manager.pool", pool),
        patch("app.repos.db_helpers.set_user_context", new_callable=AsyncMock),
        patch("app.repos.db_helpers.clear_user_context", new_callable=AsyncMock),
    ):
        await _extract_graph_from_media(
            user_id=42,
            text="A sufficiently detailed media-derived memory",
            api_key="key",
            source_memory_id=77,
            media_type="photo",
            telegram_file_id="telegram-file",
            expected_epoch=9,
        )

    sql = " ".join(conn.execute.await_args.args[0].split())
    args = conn.execute.await_args.args[1:]
    assert "memory_node_sources" in sql
    assert "long_term_memory" in sql
    assert "chats" in sql
    assert "memory_epoch" in sql
    assert "UPDATE memory_node_sources" in sql
    assert "UPDATE memory_nodes" in sql
    assert args == ("telegram-file", "photo", 42, 77, 9)


@pytest.mark.asyncio
async def test_revoked_media_capture_stops_before_external_processing():
    from app.utils.multimodal_processor import process_media_for_memory

    lease_calls: list[tuple[int, int | None, str, bool]] = []

    @asynccontextmanager
    async def denied_lease(user_id, expected_epoch, *, purpose, require_ltm):
        lease_calls.append((user_id, expected_epoch, purpose, require_ltm))
        yield False

    with (
        patch("app.repos.memory_consent.private_data_lease", denied_lease),
        patch(
            "app.utils.multimodal_processor._transcribe_voice_for_ltm",
            new_callable=AsyncMock,
        ) as transcribe,
        patch("app.repos.memory.store_memory", new_callable=AsyncMock) as store,
    ):
        result = await process_media_for_memory(
            b"private audio",
            user_id=42,
            media_type="voice",
            expected_epoch=9,
        )

    assert result is None
    assert lease_calls == [(42, 9, "ltm:media:voice", True)]
    transcribe.assert_not_awaited()
    store.assert_not_awaited()

"""Regression tests for complete, atomic GDPR account erasure."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


class _Connection:
    def __init__(self):
        self.transaction_context = _AsyncContext(self)
        self.execute = AsyncMock()

    def transaction(self):
        return self.transaction_context


class _Pool:
    def __init__(self, connection: _Connection):
        self.acquire_context = _AsyncContext(connection)

    def acquire(self):
        return self.acquire_context


@pytest.mark.asyncio
async def test_erase_user_account_is_atomic_and_cleans_legacy_non_fk_tables():
    from app.repos import users

    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True, _user_auth_cache={42: True})
    summary_tasks_stopped = False

    async def cancel_summary_tasks(user_id: int) -> int:
        nonlocal summary_tasks_stopped
        assert user_id == 42
        summary_tasks_stopped = True
        return 1

    async def query_after_task_shutdown(*_args, **_kwargs):
        assert summary_tasks_stopped, "DB erasure started before summary tasks stopped"
        if "nextval('memory_consent_epoch_seq')" in _args[0]:
            return [{"memory_epoch": 12, "ltm_enabled": True}]
        if "private_data_blocked IS TRUE" in _args[0]:
            return [{"user_id": 42}]
        return []

    query = AsyncMock(side_effect=query_after_task_shutdown)
    wait_for_leases = AsyncMock()

    with (
        patch.object(users, "db_manager", manager),
        patch.object(users, "db_query", query),
        patch.object(users, "set_user_context", new_callable=AsyncMock) as set_context,
        patch.object(users, "clear_user_context", new_callable=AsyncMock) as clear_context,
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", query),
        patch("app.repos.memory_consent.set_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.clear_user_context", new_callable=AsyncMock),
        patch.object(users, "is_admin", return_value=False),
        patch(
            "app.context.summarizer.cancel_user_summarization_tasks",
            side_effect=cancel_summary_tasks,
        ) as cancel_summaries,
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", new_callable=AsyncMock) as cancel_ltm,
        patch(
            "app.repos.memory_consent.wait_for_private_data_leases",
            wait_for_leases,
        ),
        patch("app.state.purge_user_runtime_state") as purge_state,
        patch("app.middleware.dedup.clear_user_dedup") as clear_dedup,
        patch(
            "app.group_chat.group_chat_manager.apply_account_erasure",
            new_callable=AsyncMock,
        ) as apply_groups,
    ):
        await users.erase_user_account(42)

    assert connection.transaction_context.entered is True
    assert connection.transaction_context.exited is True
    set_context.assert_awaited_once_with(42, True, conn=connection)
    clear_context.assert_awaited_once_with(conn=connection)
    cancel_summaries.assert_awaited_once_with(42)
    cancel_ltm.assert_awaited_once_with(42)

    statements = [" ".join(call.args[0].split()) for call in query.await_args_list]
    sql = "\n".join(statements)
    assert "DELETE FROM public.natal_reports" in sql
    assert "DELETE FROM public.daily_trivia_prompt_messages" in sql
    assert "DELETE FROM public.daily_trivia_super_results" in sql
    assert "DELETE FROM public.inline_boards" in sql
    assert "UPDATE public.group_chats" in sql
    assert "candidate.is_authorized = 1" in sql
    assert "DELETE FROM public.group_chats" in sql
    assert "UPDATE public.memory_nodes" in sql
    assert "UPDATE public.memory_edges" in sql
    assert statements[-1] == "DELETE FROM public.users WHERE user_id = $1"
    assert any("nextval('memory_consent_epoch_seq')" in statement for statement in statements)
    assert all(call.kwargs["conn"] is connection for call in query.await_args_list)

    assert 42 not in manager._user_auth_cache
    purge_state.assert_called_once_with(42)
    clear_dedup.assert_called_once_with(42)
    apply_groups.assert_awaited_once_with(
        42,
        affected_group_ids=set(),
        transferred_admins={},
        deleted_group_ids=set(),
    )
    wait_for_leases.assert_awaited_once_with(42, before_epoch=12, ltm_only=False)


@pytest.mark.asyncio
async def test_erase_wait_failure_compensates_before_any_destructive_delete():
    from app.repos import users

    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True, _user_auth_cache={42: True})

    async def query(sql, *_args, **_kwargs):
        if "nextval('memory_consent_epoch_seq')" in sql:
            return [{"memory_epoch": 12, "ltm_enabled": True}]
        return []

    query_mock = AsyncMock(side_effect=query)
    compensate = AsyncMock(return_value=True)

    with (
        patch.object(users, "db_manager", manager),
        patch.object(users, "db_query", query_mock),
        patch.object(users, "set_user_context", new_callable=AsyncMock),
        patch.object(users, "clear_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", query_mock),
        patch("app.repos.memory_consent.set_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.clear_user_context", new_callable=AsyncMock),
        patch.object(users, "is_admin", return_value=False),
        patch("app.context.summarizer.cancel_user_summarization_tasks", new_callable=AsyncMock),
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", new_callable=AsyncMock),
        patch(
            "app.repos.memory_consent.wait_for_private_data_leases",
            new_callable=AsyncMock,
            side_effect=RuntimeError("lease database unavailable"),
        ),
        patch("app.repos.memory_consent.restore_private_data_barrier", compensate),
    ):
        with pytest.raises(RuntimeError, match="lease database unavailable"):
            await users.erase_user_account(42)

    sql = "\n".join(call.args[0] for call in query_mock.await_args_list)
    assert "DELETE FROM public.users" not in sql
    compensate.assert_awaited_once_with(42, barrier_epoch=12, ltm_enabled=True)
    assert manager._user_auth_cache == {42: True}


@pytest.mark.asyncio
async def test_erase_user_account_locks_before_any_database_read_or_write():
    """Account erasure must serialize with every per-user LTM writer."""
    from app.repos import users

    events: list[tuple[str, object]] = []
    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True, _user_auth_cache={})

    async def execute(sql: str, *args):
        events.append(("execute", (" ".join(sql.split()), args)))

    async def set_context(*_args, **_kwargs):
        events.append(("context", None))

    async def query(sql, _params=(), **_kwargs):
        normalized = " ".join(sql.split())
        events.append(("query", normalized))
        if "nextval('memory_consent_epoch_seq')" in normalized:
            return [{"memory_epoch": 12, "ltm_enabled": True}]
        if "private_data_blocked IS TRUE" in normalized:
            return [{"user_id": 42}]
        return []

    connection.execute.side_effect = execute
    with (
        patch.object(users, "db_manager", manager),
        patch.object(users, "db_query", side_effect=query),
        patch.object(users, "set_user_context", side_effect=set_context),
        patch.object(users, "clear_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", side_effect=query),
        patch("app.repos.memory_consent.set_user_context", side_effect=set_context),
        patch("app.repos.memory_consent.clear_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.wait_for_private_data_leases", new_callable=AsyncMock),
        patch.object(users, "is_admin", return_value=False),
        patch("app.context.summarizer.cancel_user_summarization_tasks", new_callable=AsyncMock),
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", new_callable=AsyncMock),
        patch("app.state.purge_user_runtime_state"),
        patch("app.middleware.dedup.clear_user_dedup"),
        patch("app.group_chat.group_chat_manager.apply_account_erasure", new_callable=AsyncMock),
    ):
        await users.erase_user_account(42)

    lock_indices = [
        index for index, event in enumerate(events) if event == ("execute", ("SELECT pg_advisory_xact_lock($1)", (42,)))
    ]
    assert len(lock_indices) == 2
    assert all(events[index - 1][0] == "context" for index in lock_indices)
    assert all(events[index + 1][0] == "query" for index in lock_indices)


@pytest.mark.asyncio
async def test_erase_user_account_propagates_database_failure_without_false_cache_cleanup():
    from app.repos import users

    connection = _Connection()
    manager = SimpleNamespace(pool=_Pool(connection), is_connected=True, _user_auth_cache={42: True})
    query = AsyncMock(side_effect=RuntimeError("db down"))

    with (
        patch.object(users, "db_manager", manager),
        patch.object(users, "db_query", query),
        patch.object(users, "set_user_context", new_callable=AsyncMock),
        patch.object(users, "clear_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.db_manager", manager),
        patch("app.repos.memory_consent.db_query", query),
        patch("app.repos.memory_consent.set_user_context", new_callable=AsyncMock),
        patch("app.repos.memory_consent.clear_user_context", new_callable=AsyncMock),
        patch.object(users, "is_admin", return_value=False),
        patch("app.repos.memory_autosave.cancel_user_memory_tasks", new_callable=AsyncMock),
        patch("app.state.purge_user_runtime_state") as purge_state,
        patch("app.middleware.dedup.clear_user_dedup") as clear_dedup,
        patch(
            "app.group_chat.group_chat_manager.apply_account_erasure",
            new_callable=AsyncMock,
        ) as apply_groups,
    ):
        with pytest.raises(RuntimeError, match="db down"):
            await users.erase_user_account(42)

    assert manager._user_auth_cache == {42: True}
    purge_state.assert_not_called()
    clear_dedup.assert_not_called()
    apply_groups.assert_not_awaited()


@pytest.mark.asyncio
async def test_erase_user_account_rejects_configured_admin():
    from app.repos import users

    with (
        patch.object(users, "is_admin", return_value=True),
        patch.object(users, "db_manager") as manager,
    ):
        with pytest.raises(ValueError, match="administrator"):
            await users.erase_user_account(42)

    manager.pool.acquire.assert_not_called()


def test_purge_user_runtime_state_cancels_pending_writes_and_drops_all_maps():
    from app import state

    user_id = 987_654
    state.USER_STATES._states[user_id] = state.UserState(user_id)
    timer = MagicMock()
    task = MagicMock()
    task.done.return_value = False
    state._pending_persists[user_id] = timer
    state._ACTIVE_TASKS[user_id] = task
    state._NETWORK_STALL_SINCE[user_id] = 1.0
    state._LAST_BOT_MESSAGE[user_id] = (10, 20)

    state.purge_user_runtime_state(user_id)

    timer.cancel.assert_called_once_with()
    task.cancel.assert_called_once_with()
    assert user_id not in state.USER_STATES._states
    assert user_id not in state._pending_persists
    assert user_id not in state._ACTIVE_TASKS
    assert user_id not in state._NETWORK_STALL_SINCE
    assert user_id not in state._LAST_BOT_MESSAGE


def _private_update(text: str = "/deleteme CONFIRM", user_id: int = 42):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.type = "private"
    update.message = AsyncMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_deleteme_confirmation_uses_complete_erasure_and_reports_success_only_after_commit():
    from app.handlers.commands import deleteme_command

    update = _private_update()
    with (
        patch("app.repos.users.is_admin", return_value=False),
        patch("app.repos.users.erase_user_account", new_callable=AsyncMock) as erase,
        patch("app.handlers.commands.TelegramFormatter") as formatter,
    ):
        formatter.format_text.return_value = ("complete-erasure", "Markdown")
        await deleteme_command.__wrapped__.__wrapped__(update, MagicMock())

    erase.assert_awaited_once_with(42)
    assert update.message.reply_text.await_count == 2
    assert update.message.reply_text.await_args_list[-1].args[0] == "complete-erasure"
    formatted_source = formatter.format_text.call_args.args[0]
    assert "аккаунт" in formatted_source.lower()
    assert "/start" not in formatted_source


@pytest.mark.asyncio
async def test_deleteme_database_failure_never_reports_success():
    from app.handlers.commands import deleteme_command

    update = _private_update()
    with (
        patch("app.repos.users.is_admin", return_value=False),
        patch(
            "app.repos.users.erase_user_account",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        patch("app.handlers.commands.TelegramFormatter") as formatter,
    ):
        await deleteme_command.__wrapped__.__wrapped__(update, MagicMock())

    formatter.format_text.assert_not_called()
    assert update.message.reply_text.await_count == 2
    assert "не удалось" in update.message.reply_text.await_args_list[-1].args[0].lower()


@pytest.mark.asyncio
async def test_deleteme_is_private_only_and_protects_system_admin():
    from app.handlers.commands import deleteme_command

    group_update = _private_update()
    group_update.effective_chat.type = "group"
    admin_update = _private_update(user_id=1)

    with (
        patch("app.repos.users.is_admin", side_effect=lambda user_id: user_id == 1),
        patch("app.repos.users.erase_user_account", new_callable=AsyncMock) as erase,
    ):
        await deleteme_command.__wrapped__.__wrapped__(group_update, MagicMock())
        await deleteme_command.__wrapped__.__wrapped__(admin_update, MagicMock())

    erase.assert_not_awaited()
    assert "приват" in group_update.message.reply_text.await_args.args[0].lower()
    assert "администратор" in admin_update.message.reply_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_clearmemory_database_failure_never_reports_success():
    from app.handlers.commands import clearmemory_command

    update = _private_update(text="/clearmemory")
    with (
        patch(
            "app.repos.memory.delete_user_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ),
        patch("app.handlers.commands.TelegramFormatter") as formatter,
    ):
        await clearmemory_command.__wrapped__.__wrapped__(update, MagicMock())

    formatter.format_text.assert_not_called()
    update.message.reply_text.assert_awaited_once()
    assert "не удалось" in update.message.reply_text.await_args.args[0].lower()


def test_account_erasure_migration_adds_database_level_ownership_constraints():
    migration = Path("scripts/migrations/068_harden_account_erasure.sql").read_text(encoding="utf-8")
    normalized = " ".join(migration.split()).lower()

    for column in (
        "natal_reports_user_fk",
        "daily_trivia_prompt_messages_user_fk",
        "daily_trivia_super_results_user_fk",
        "inline_boards_creator_fk",
    ):
        assert column in normalized
    assert normalized.count("on delete cascade") >= 4
    assert "group_chats_admin_user_fk" in normalized
    assert "on delete restrict" in normalized
    assert "memory_nodes_actor_user_fk" in normalized
    assert "memory_edges_actor_user_fk" in normalized
    assert normalized.count("on delete set null") >= 2
    assert "set is_admin = true" in normalized
    assert "candidate.is_authorized = 1" in normalized

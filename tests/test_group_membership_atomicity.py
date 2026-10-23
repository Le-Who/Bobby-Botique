"""Group counts and membership changes commit together before cache updates."""

import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import group_chat


@pytest.fixture
def group_store(monkeypatch):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE group_chats (chat_id INTEGER PRIMARY KEY, title TEXT, admin_user_id INTEGER, member_count INTEGER DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE group_members (chat_id INTEGER, user_id INTEGER, is_admin BOOLEAN DEFAULT FALSE, PRIMARY KEY (chat_id, user_id))"
    )

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            connection.execute("BEGIN")
            try:
                yield
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    bound = Connection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield bound

    async def query(sql, params=(), *, conn=None):
        assert conn is None or conn is bound
        cursor = connection.execute(sql, {str(index): value for index, value in enumerate(params, start=1)})
        return [dict(row) for row in cursor.fetchall()] if cursor.description else []

    monkeypatch.setattr(group_chat.GroupChatManager, "_instance", None)
    manager = group_chat.GroupChatManager()
    monkeypatch.setattr(group_chat, "_is_authorized", AsyncMock(return_value=True))
    monkeypatch.setattr(group_chat.db, "db_manager", SimpleNamespace(is_connected=True, pool=Pool()))
    monkeypatch.setattr(group_chat.db, "db_query", query)
    yield SimpleNamespace(db=connection, manager=manager)
    connection.close()


def _seed_group(store):
    store.db.execute("INSERT INTO group_chats VALUES (100, 'Group', 1, 2)")
    store.db.execute("INSERT INTO group_members VALUES (100, 1, TRUE), (100, 2, FALSE)")
    store.manager.active_groups[100] = group_chat.GroupChat(100, "Group", True, None, None, 2, 1, {})
    store.manager.user_groups[1].add(100)
    store.manager.user_groups[2].add(100)


@pytest.mark.asyncio
async def test_repeated_member_removal_preserves_other_members_count(group_store):
    _seed_group(group_store)

    assert await group_store.manager.remove_member_from_group(100, 2)
    assert await group_store.manager.remove_member_from_group(100, 2)

    assert group_store.db.execute("SELECT member_count FROM group_chats").fetchone()[0] == 1
    assert group_store.manager.active_groups[100].member_count == 1
    assert group_store.manager.user_groups[1] == {100}
    assert not group_store.manager.user_groups[2]


@pytest.mark.asyncio
async def test_group_registration_persists_initial_admin_count(group_store):
    assert await group_store.manager.register_group(100, "Group", 1)

    assert group_store.db.execute("SELECT member_count FROM group_chats").fetchone()[0] == 1
    assert group_store.db.execute("SELECT user_id, is_admin FROM group_members").fetchone()[:] == (1, 1)


@pytest.mark.asyncio
async def test_failed_admin_insert_rolls_back_new_group(group_store):
    group_store.db.execute(
        "CREATE TRIGGER fail_admin BEFORE INSERT ON group_members BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
    )

    assert not await group_store.manager.register_group(100, "Group", 1)

    assert group_store.db.execute("SELECT count(*) FROM group_chats").fetchone()[0] == 0
    assert 100 not in group_store.manager.active_groups
    assert not group_store.manager.user_groups.get(1)


@pytest.mark.asyncio
async def test_failed_count_update_rolls_back_added_member(group_store):
    _seed_group(group_store)
    group_store.db.execute(
        "CREATE TRIGGER fail_count BEFORE UPDATE ON group_chats BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
    )

    assert not await group_store.manager.add_member_to_group(100, 3)

    assert group_store.db.execute("SELECT count(*) FROM group_members WHERE user_id = 3").fetchone()[0] == 0
    assert group_store.manager.active_groups[100].member_count == 2
    assert not group_store.manager.user_groups.get(3)


@pytest.mark.asyncio
async def test_failed_count_update_rolls_back_removed_member(group_store):
    _seed_group(group_store)
    group_store.db.execute(
        "CREATE TRIGGER fail_count BEFORE UPDATE ON group_chats BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
    )

    assert not await group_store.manager.remove_member_from_group(100, 2)

    assert group_store.db.execute("SELECT count(*) FROM group_members WHERE user_id = 2").fetchone()[0] == 1
    assert group_store.manager.active_groups[100].member_count == 2
    assert group_store.manager.user_groups[2] == {100}

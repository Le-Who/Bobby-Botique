from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.group_chat import GroupChat, GroupChatManager


@pytest.fixture
def group_chat_manager():
    # Reset singleton instance
    GroupChatManager._instance = None
    manager = GroupChatManager()
    return manager

@pytest.mark.asyncio
async def test_singleton_behavior():
    GroupChatManager._instance = None
    manager1 = GroupChatManager()
    manager2 = GroupChatManager()
    assert manager1 is manager2
    assert manager1.active_groups is manager2.active_groups

@pytest.mark.asyncio
async def test_initialize(group_chat_manager):
    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:
        # Setup mock return for initialize
        # 1. create group_chats
        # 2. create group_members
        # 3. create group_messages
        # 4. load active groups (returns empty list)
        mock_db_query.side_effect = [
            None,
            None,
            None,
            [],
        ]

        await group_chat_manager.initialize()

        assert mock_db_query.call_count == 4
        # Verify tables creation queries
        assert "CREATE TABLE IF NOT EXISTS group_chats" in mock_db_query.call_args_list[0][0][0]

@pytest.mark.asyncio
async def test_register_group(group_chat_manager):
    chat_id = 100
    title = "Test Group"
    admin_id = 1

    with patch("app.group_chat._is_authorized", new_callable=AsyncMock) as mock_auth, \
         patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:

        mock_auth.return_value = True

        result = await group_chat_manager.register_group(chat_id, title, admin_id)

        assert result is True
        assert chat_id in group_chat_manager.active_groups
        assert group_chat_manager.active_groups[chat_id].title == title
        assert group_chat_manager.active_groups[chat_id].member_count == 1
        assert chat_id in group_chat_manager.user_groups[admin_id]

        # Verify DB calls
        # 1. INSERT group_chats
        # 2. INSERT group_members
        assert mock_db_query.call_count == 2
        assert "INSERT INTO group_chats" in mock_db_query.call_args_list[0][0][0]
        assert "INSERT INTO group_members" in mock_db_query.call_args_list[1][0][0]

@pytest.mark.asyncio
async def test_add_member_to_group(group_chat_manager):
    chat_id = 100
    user_id = 2

    # Pre-register a group
    group = GroupChat(
        chat_id=chat_id,
        title="Test Group",
        is_active=True,
        created_at=None,
        last_activity=None,
        member_count=1,
        admin_user_id=1,
        settings={}
    )
    group_chat_manager.active_groups[chat_id] = group

    with patch("app.group_chat._is_authorized", new_callable=AsyncMock) as mock_auth, \
         patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:

        mock_auth.return_value = True

        result = await group_chat_manager.add_member_to_group(chat_id, user_id)

        assert result is True
        assert group_chat_manager.active_groups[chat_id].member_count == 2
        assert chat_id in group_chat_manager.user_groups[user_id]

        # Verify DB calls
        assert mock_db_query.call_count == 2
        assert "INSERT INTO group_members" in mock_db_query.call_args_list[0][0][0]
        assert "UPDATE group_chats" in mock_db_query.call_args_list[1][0][0]

@pytest.mark.asyncio
async def test_remove_member_from_group(group_chat_manager):
    chat_id = 100
    user_id = 2

    # Pre-register a group and member
    group = GroupChat(
        chat_id=chat_id,
        title="Test Group",
        is_active=True,
        created_at=None,
        last_activity=None,
        member_count=2,
        admin_user_id=1,
        settings={}
    )
    group_chat_manager.active_groups[chat_id] = group
    group_chat_manager.user_groups[user_id].add(chat_id)

    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:

        result = await group_chat_manager.remove_member_from_group(chat_id, user_id)

        assert result is True
        assert group_chat_manager.active_groups[chat_id].member_count == 1
        assert chat_id not in group_chat_manager.user_groups[user_id]

        # Verify DB calls
        assert mock_db_query.call_count == 2
        assert "DELETE FROM group_members" in mock_db_query.call_args_list[0][0][0]
        assert "UPDATE group_chats" in mock_db_query.call_args_list[1][0][0]

@pytest.mark.asyncio
async def test_get_group_stats(group_chat_manager):
    chat_id = 100

    # Pre-register a group
    group = GroupChat(
        chat_id=chat_id,
        title="Test Group",
        is_active=True,
        created_at=None,
        last_activity=None,
        member_count=5,
        admin_user_id=1,
        settings={}
    )
    group_chat_manager.active_groups[chat_id] = group

    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:
        mock_db_query.side_effect = [
            [{"count": 100}], # total_messages
            [{"count": 10}],  # recent_messages
            [{"count": 5}],   # active_users
        ]

        stats = await group_chat_manager.get_group_stats(chat_id)

        assert stats["total_messages"] == 100
        assert stats["recent_messages"] == 10
        assert stats["active_users_24h"] == 5
        assert stats["member_count"] == 5

@pytest.mark.asyncio
async def test_log_group_message(group_chat_manager):
    chat_id = 100
    user_id = 1
    message_text = "Hello"

    # Pre-register a group
    group = GroupChat(
        chat_id=chat_id,
        title="Test Group",
        is_active=True,
        created_at=None,
        last_activity=None,
        member_count=1,
        admin_user_id=1,
        settings={}
    )
    group_chat_manager.active_groups[chat_id] = group

    with patch("app.database.db_query", new_callable=AsyncMock) as mock_db_query:

        await group_chat_manager.log_group_message(chat_id, user_id, message_text)

        assert mock_db_query.call_count == 2
        assert "INSERT INTO group_messages" in mock_db_query.call_args_list[0][0][0]
        assert "UPDATE group_chats" in mock_db_query.call_args_list[1][0][0]

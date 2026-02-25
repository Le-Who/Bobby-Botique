import sys
import os
import asyncio
from unittest.mock import AsyncMock, patch
import pytest

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def get_database():
    import importlib
    import app.database

    if "app.database" not in sys.modules or isinstance(
        sys.modules["app.database"], type(patch)
    ):
        importlib.reload(app)
    from app import database

    return database


async def test_get_conversation_messages_optimization():
    # Setup Mock
    mock_db_query = AsyncMock()

    # Configure mock to return specific results based on query content
    async def side_effect(query, *args, **kwargs):
        query_lower = query.lower()
        # Extract conv_id from args tuple (query, (conv_id, user_id))
        params = args[0]
        conv_id = params[0]

        # Check if query uses LEFT JOIN (new implementation)
        if "left join" in query_lower:
            # Case 1: Valid conversation with messages (id=123)
            if conv_id == 123:
                return [
                    {"role": "user", "content": "hello", "created_at": "2023-01-01"},
                    {"role": "assistant", "content": "hi", "created_at": "2023-01-01"},
                ]
            # Case 2: Valid conversation, no messages (id=456)
            elif conv_id == 456:
                # Returns 1 row with NULLs if conversation exists but no messages
                return [{"role": None, "content": None, "created_at": None}]
            # Case 3: Invalid conversation (id=999)
            elif conv_id == 999:
                return []
        else:
            # Fallback for old implementation (if test runs before fix)
            # But we are testing the new logic mostly.
            pass

        return []

    mock_db_query.side_effect = side_effect

    # Patch the function in the canonical repos module
    from app.repos import conversations as conv_module
    database = get_database()
    with patch.object(conv_module, "db_query", mock_db_query):
        # 1. Valid Conversation with Messages
        messages = await database.get_conversation_messages(123, 1)
        assert messages is not None
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert mock_db_query.call_count == 1
        mock_db_query.reset_mock()

        # 2. Valid Conversation, Empty (Should return [])
        messages = await database.get_conversation_messages(456, 1)
        assert messages is not None
        assert isinstance(messages, list)
        assert len(messages) == 0
        assert mock_db_query.call_count == 1
        mock_db_query.reset_mock()

        # 3. Invalid Conversation (Should return None)
        messages = await database.get_conversation_messages(999, 1)
        assert messages is None
        assert mock_db_query.call_count == 1


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(test_get_conversation_messages_optimization())

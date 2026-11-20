import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


async def test_get_conversation_messages_optimization():
    mock_db_query = AsyncMock()

    async def side_effect(query, *args, **kwargs):
        query_lower = query.lower()
        params = args[0]
        conv_id = params[0]

        if "left join" in query_lower:
            if conv_id == 123:
                return [
                    {"role": "user", "content": "hello", "created_at": "2023-01-01"},
                    {"role": "assistant", "content": "hi", "created_at": "2023-01-01"},
                ]
            elif conv_id == 456:
                return [{"role": None, "content": None, "created_at": None}]
            elif conv_id == 999:
                return []
        else:
            pass

        return []

    mock_db_query.side_effect = side_effect

    from app.repos import conversations as conv_module

    with patch.object(conv_module, "db_query", mock_db_query):
        messages = await conv_module.get_conversation_messages(123, 1)
        assert messages is not None
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert mock_db_query.call_count == 1
        mock_db_query.reset_mock()

        messages = await conv_module.get_conversation_messages(456, 1)
        assert messages is not None
        assert isinstance(messages, list)
        assert len(messages) == 0
        assert mock_db_query.call_count == 1
        mock_db_query.reset_mock()

        messages = await conv_module.get_conversation_messages(999, 1)
        assert messages is None
        assert mock_db_query.call_count == 1


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(test_get_conversation_messages_optimization())

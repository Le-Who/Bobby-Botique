from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import ChatState
from app.repos import chats


def _pool_boundary():
    conn = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return conn, SimpleNamespace(pool=pool, is_connected=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dropped_count", "retained_roles"),
    [
        (1, ["model", "user", "model"]),
        (2, ["user", "model"]),
    ],
)
async def test_compacted_history_forces_full_rewrite_regardless_of_final_length(
    dropped_count,
    retained_roles,
):
    """Dropping 1/2 turns cannot be mistaken for append-only/equal length."""
    history = [
        {"role": role, "parts": [f"content-{index}"]}
        for index, role in enumerate([*retained_roles, "user", "model"])
    ]
    state = ChatState(
        history=history,
        model="gemini-3.1-flash-lite",
        token_count=10,
        search_enabled=False,
        system_prompt=None,
        _original_length=4,
    )
    conn, manager = _pool_boundary()

    with (
        patch.object(chats, "db_manager", manager),
        patch.object(chats, "db_query", new_callable=AsyncMock) as query,
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
    ):
        await chats.update_user_chat(42, state, rewrite_history=True)

    params = query.await_args.args[1]
    assert params[10] is True
    assert params[11] == [message["role"] for message in history]
    assert params[12] == [message["parts"][0] for message in history]
    assert state._original_length == 6 - dropped_count
    assert query.await_args.kwargs["conn"] is conn

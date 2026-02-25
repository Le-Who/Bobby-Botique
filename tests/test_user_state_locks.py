import asyncio

from app import state


def test_user_lock_single_source_of_truth():
    user_id = 123456

    lock_from_getter = state.get_user_lock(user_id)
    lock_from_state = state.get_user_state(user_id).lock

    assert isinstance(lock_from_getter, asyncio.Lock)
    assert lock_from_getter is lock_from_state

"""Private-data boundary shared by owned role and Tarot requests."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from app.repos import chats, memory_consent
from app.state import InteractionOwner, UserState, get_user_state, is_ordinary_request_busy


async def reject_busy_interaction(user_id: int, reply: Callable[[str], Awaitable[Any]]) -> bool:
    """Keep role/Tarot starts from overlapping an admitted ordinary request."""
    if not is_ordinary_request_busy(user_id):
        return False
    await reply("⏳ Дождитесь завершения текущего запроса или отмените его.")
    return True


class TextInputIntent:
    """Own an explicit input transition before its first asynchronous boundary."""

    def __init__(self, state: UserState):
        self.state = state

    def current(self) -> bool:
        return self.state._user_id != 0 and self.state.text_input_intent is self


def begin_text_input_intent(user_id: int) -> TextInputIntent | None:
    if is_ordinary_request_busy(user_id):
        return None
    state = get_user_state(user_id)
    intent = TextInputIntent(state)
    state.text_input_intent = intent
    return intent


def retire_context_text_input(user_data) -> None:
    """Retire only local next-text drafts; keep canvas settings and voice data."""
    if user_data is None:
        return
    from app.handlers.msg_roles import clear_role_edit_state

    clear_role_edit_state(user_data)
    for key in ("rename_conv_id", "rename_role_id", "rename_role_key", "_text_input_owner"):
        user_data.pop(key, None)
    draw = user_data.get("draw_state")
    if isinstance(draw, dict):
        draw["awaiting_prompt"] = False


def bind_context_text_input(owner: InteractionOwner, user_data) -> None:
    """Let reset/retirement clear the exact input draft without a PTB context."""
    if user_data is None:
        return
    user_data["_text_input_owner"] = owner

    def retire() -> None:
        if user_data.get("_text_input_owner") is owner:
            retire_context_text_input(user_data)

    owner.on_retire = retire


async def prepare_text_input(
    user_id: int, message, reply: Callable[[str], Awaitable[Any]], user_data, *, input_intent: TextInputIntent | None
) -> bool:
    """Drain the previous input mode before accepting a new role/draw/rename."""
    if input_intent is None:
        await reply("⏳ Дождитесь завершения текущего запроса или отмените его.")
        return False
    if not input_intent.current() or await reject_busy_interaction(user_id, reply):
        return False
    from app.handlers.tarot_chat import leave_tarot_for_chat
    from app.state import cancel_interaction, ensure_state_loaded

    await ensure_state_loaded(user_id)
    if not input_intent.current():
        return False
    await leave_tarot_for_chat(user_id, message, input_intent=input_intent)
    if not input_intent.current() or await reject_busy_interaction(user_id, reply):
        return False
    await cancel_interaction(user_id, input_intent=input_intent)
    if not input_intent.current() or await reject_busy_interaction(user_id, reply):
        return False
    retire_context_text_input(user_data)
    return True


async def interaction_current(owner: InteractionOwner) -> bool:
    if not owner.current() or owner.epoch is None:
        return False
    current = await memory_consent.is_private_data_snapshot_current(
        owner.state._user_id, owner.epoch, require_ltm=False
    )
    return current and owner.current()


@asynccontextmanager
async def private_interaction(owner: InteractionOwner, chat_state=None) -> AsyncIterator[bool]:
    user_id = owner.state._user_id
    if not owner.current():
        yield False
        return
    if owner.epoch is None:
        if chat_state is None:
            chat_state = await chats.get_user_chat(user_id)
        if not owner.current():
            yield False
            return
        known_epoch = (
            getattr(chat_state, "memory_epoch", 0) if getattr(chat_state, "_has_persisted_chat", True) else None
        )
        owner.epoch = await chats.ensure_chat_generation(user_id, expected_epoch=known_epoch)
    if not owner.current() or owner.epoch is None:
        yield False
        return
    if chat_state is not None and getattr(chat_state, "_has_persisted_chat", True) is False:
        # The first-use generation was created above. Keep subsequent writes
        # tied to it rather than taking update_user_chat's unconditional path.
        chat_state.memory_epoch = owner.epoch
        chat_state._has_persisted_chat = True
    async with memory_consent.private_data_lease(
        user_id, owner.epoch, purpose=f"interaction:{owner.kind}", require_ltm=False
    ) as allowed:
        yield allowed and owner.current()

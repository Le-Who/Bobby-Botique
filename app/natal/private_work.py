"""Capture a durable account generation before accepting natal input work."""


async def capture_natal_epoch(user_id: int) -> int | None:
    """Initialize a first chat without replacing existing state or LTM consent."""
    from app.repos.chats import ensure_chat_generation, get_user_chat

    chat = await get_user_chat(user_id)
    if chat is None:
        return None
    known_epoch = int(chat.memory_epoch) if getattr(chat, "_has_persisted_chat", False) is True else None
    return await ensure_chat_generation(user_id, expected_epoch=known_epoch)

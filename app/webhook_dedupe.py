"""Cross-process Telegram webhook update dedupe."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping

try:
    from app.cache import redis_client
except Exception:  # pragma: no cover - defensive fallback during partial imports
    redis_client = None

logger = logging.getLogger(__name__)

_DEDUP_KEY_PREFIX = "telegram:webhook:update:"
_COMMAND_DEDUP_KEY_PREFIX = "telegram:webhook:cmd:"


def _command_identity_key(payload: Mapping[str, object] | None) -> str | None:
    if not payload:
        return None

    message_obj = payload.get("message") or payload.get("edited_message")
    if not isinstance(message_obj, Mapping):
        return None

    text = message_obj.get("text")
    entities = message_obj.get("entities")
    if not isinstance(text, str) or not isinstance(entities, list) or not entities:
        return None

    first_entity = entities[0]
    if not isinstance(first_entity, Mapping):
        return None
    if first_entity.get("type") != "bot_command" or first_entity.get("offset") != 0:
        return None

    length = first_entity.get("length")
    if not isinstance(length, int) or length <= 1:
        return None

    message_id = message_obj.get("message_id")
    chat = message_obj.get("chat")
    if not isinstance(message_id, int) or not isinstance(chat, Mapping):
        return None

    chat_id = chat.get("id")
    chat_type = chat.get("type", "unknown")
    if not isinstance(chat_id, int):
        return None

    command = text[1:length].split("@", 1)[0].lower()
    if not command:
        return None

    return f"{_COMMAND_DEDUP_KEY_PREFIX}{chat_type}:{chat_id}:{message_id}:{command}"


async def should_accept_webhook_update(
    update_id: int,
    seen_update_ids: dict[object, float],
    seen_lock: asyncio.Lock,
    *,
    payload: Mapping[str, object] | None = None,
    ttl_seconds: float = 180.0,
    capacity: int = 10_000,
) -> bool:
    """Return False when a Telegram update was already claimed.

    Redis is the primary guard so overlapping old/new bot containers cannot both
    process the same webhook update during deploys. The in-process map remains a
    fallback for local/dev or degraded Redis.
    """
    ttl = max(1, int(ttl_seconds))
    keys: list[object] = [update_id]
    command_key = _command_identity_key(payload)
    if command_key:
        keys.append(command_key)

    if redis_client:
        try:
            for key in keys:
                redis_key = f"{_DEDUP_KEY_PREFIX}{key}" if isinstance(key, int) else str(key)
                claimed = await redis_client.set(redis_key, "1", ex=ttl, nx=True)
                if not claimed:
                    return False
            return True
        except Exception as exc:
            logger.warning("Webhook Redis dedupe unavailable; using local fallback: %s", exc)

    now = time.monotonic()
    async with seen_lock:
        stale = [uid for uid, ts in seen_update_ids.items() if now - ts > ttl_seconds]
        for uid in stale:
            seen_update_ids.pop(uid, None)

        if update_id in seen_update_ids:
            return False
        if command_key and command_key in seen_update_ids:
            return False

        if len(seen_update_ids) >= capacity:
            oldest = sorted(seen_update_ids.items(), key=lambda item: item[1])[: max(1, capacity // 10)]
            for uid, _ in oldest:
                seen_update_ids.pop(uid, None)
        for key in keys:
            seen_update_ids[key] = now
        return True

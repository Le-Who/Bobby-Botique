"""Cross-process Telegram webhook update dedupe."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping

try:
    from app.cache import redis_client
except Exception:  # pragma: no cover - defensive fallback during partial imports
    redis_client = None

logger = logging.getLogger(__name__)

_DEDUP_KEY_PREFIX = "telegram:webhook:update:"
_COMMAND_DEDUP_KEY_PREFIX = "telegram:webhook:cmd:"


class WebhookDedupeUnavailable(Exception):
    """Distributed admission did not receive a definitive claim result."""


_CONFIRM_ENQUEUED = """
for _, key in ipairs(KEYS) do
    if redis.call('GET', key) ~= ARGV[1] then return 0 end
end
for _, key in ipairs(KEYS) do redis.call('SET', key, ARGV[2], 'EX', ARGV[3]) end
return 1
"""
_RELEASE_PENDING = """
for _, key in ipairs(KEYS) do
    if redis.call('GET', key) == ARGV[1] then redis.call('DEL', key) end
end
return 1
"""


class WebhookUpdateClaim:
    """A pending reservation becomes accepted only after the local queue handoff."""

    def __init__(
        self, keys: list[object], history: dict[object, float], lock: asyncio.Lock, ttl_seconds: float, capacity: int
    ) -> None:
        self.keys = keys
        self.history = history
        self.lock = lock
        self.ttl_seconds = ttl_seconds
        self.capacity = max(1, capacity)
        self.client = redis_client
        self.token = uuid.uuid4().hex
        self.pending = "pending:" + self.token

    @property
    def redis_keys(self) -> list[str]:
        return [f"{_DEDUP_KEY_PREFIX}{key}" if isinstance(key, int) else str(key) for key in self.keys]

    async def release(self) -> None:
        """Release only our unqueued reservation; uncertainty remains pending."""
        if self.client:
            try:
                async with asyncio.timeout(5.0):
                    await self.client.eval(_RELEASE_PENDING, len(self.keys), *self.redis_keys, self.pending)
            except Exception as exc:
                logger.warning("Webhook reservation release unavailable type=%s", type(exc).__name__)

    async def confirm_enqueued(self) -> None:
        # Queue acceptance is definitive even if the subsequent Redis ACK is
        # lost. Retain that local evidence for this process's future retries.
        async with self.lock:
            now = time.monotonic()
            remembered = self.keys[-self.capacity :]
            overflow = len(self.history) + sum(key not in self.history for key in remembered) - self.capacity
            if overflow > 0:
                oldest = sorted(
                    ((key, ts) for key, ts in self.history.items() if key not in remembered), key=lambda item: item[1]
                )[: max(overflow, max(1, self.capacity // 10))]
                for key, _ in oldest:
                    self.history.pop(key, None)
            for key in remembered:
                self.history[key] = now
        if self.client:
            try:
                async with asyncio.timeout(5.0):
                    confirmed = await self.client.eval(
                        _CONFIRM_ENQUEUED,
                        len(self.keys),
                        *self.redis_keys,
                        self.pending,
                        "accepted:" + self.token,
                        max(1, int(self.ttl_seconds)),
                    )
                if confirmed != 1:
                    raise WebhookDedupeUnavailable("Webhook reservation replaced before acknowledgment")
            except Exception as exc:
                raise WebhookDedupeUnavailable from exc


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


async def claim_webhook_update(
    update_id: int,
    seen_update_ids: dict[object, float],
    seen_lock: asyncio.Lock,
    *,
    payload: Mapping[str, object] | None = None,
    ttl_seconds: float = 180.0,
    capacity: int = 10_000,
) -> WebhookUpdateClaim | None:
    """Reserve an update or return None for a confirmed queue admission.

    Redis is the primary guard so overlapping old/new bot containers cannot both
    process the same webhook update during deploys. The in-process map remains a
    bounded history of confirmed admissions and supports no-Redis local/dev mode.
    Pending, legacy and unknown claims remain retryable. An accepted marker is
    bounded evidence of the local queue handoff, not crash-durable processing.
    """
    ttl = max(1, int(ttl_seconds))
    keys: list[object] = [update_id]
    command_key = _command_identity_key(payload)
    if command_key:
        keys.append(command_key)

    claim = WebhookUpdateClaim(keys, seen_update_ids, seen_lock, ttl_seconds, capacity)
    redis_failure = None
    if redis_client:
        try:
            for redis_key in claim.redis_keys:
                claimed = await redis_client.set(redis_key, claim.pending, ex=ttl, nx=True)
                if not claimed:
                    value = await redis_client.get(redis_key)
                    if isinstance(value, bytes):
                        value = value.decode("ascii", errors="replace")
                    if isinstance(value, str) and value.startswith("accepted:"):
                        await claim.release()
                        return None
                    raise WebhookDedupeUnavailable("Webhook reservation is pending or unknown")
        except Exception as exc:
            redis_failure = exc
            logger.warning("Webhook Redis dedupe unavailable; deferring new admission type=%s", type(exc).__name__)

    now = time.monotonic()
    async with seen_lock:
        stale = [uid for uid, ts in seen_update_ids.items() if now - ts > ttl_seconds]
        for uid in stale:
            seen_update_ids.pop(uid, None)

        if (not redis_client or redis_failure is not None) and any(key in seen_update_ids for key in keys):
            await claim.release()
            return None
        if redis_failure is not None:
            raise WebhookDedupeUnavailable from redis_failure

        return claim

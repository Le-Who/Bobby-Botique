from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from app.cache import redis_client
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

_RUNTIME_TTL_S = 2 * 24 * 60 * 60
_EVENTS_CHANNEL_PREFIX = "croc:runtime:events:"
_HINTS_KEY_PREFIX = "croc:runtime:hints:"
_HISTORY_KEY_PREFIX = "croc:runtime:history:"
_LOCK_KEY_PREFIX = "croc:lock:"
_GAME_LOCKS_MAX = 512

_local_hints: dict[str, list[str]] = {}
_local_history: dict[str, list[dict]] = {}
_local_subscribers: dict[str, dict[str, asyncio.Queue]] = defaultdict(dict)  # type: ignore[type-arg]
_local_locks: dict[str, asyncio.Lock] = {}
_game_locks = _local_locks


def _decode_text(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _hints_key(game_id: str) -> str:
    return f"{_HINTS_KEY_PREFIX}{game_id}"


def _history_key(game_id: str) -> str:
    return f"{_HISTORY_KEY_PREFIX}{game_id}"


def _events_channel(game_id: str) -> str:
    return f"{_EVENTS_CHANNEL_PREFIX}{game_id}"


def _lock_key(game_id: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{game_id}"


def _sweep_game_locks() -> None:
    """Bound local fallback lock growth when Redis locking is unavailable."""
    if len(_local_locks) < _GAME_LOCKS_MAX:
        return

    keys = list(_local_locks.keys())
    removed = len(keys) // 2
    for key in keys[:removed]:
        _local_locks.pop(key, None)
    logger.debug("Runtime local locks swept: %d entries removed", removed)


async def get_runtime_hints(game_id: str) -> list[str]:
    if game_id in _local_hints:
        return list(_local_hints[game_id])

    if redis_client:
        try:
            raw = await redis_client.get(_hints_key(game_id))  # type: ignore[misc]
            text = _decode_text(raw)
            if text:
                hints = json.loads(text)
                if isinstance(hints, list):
                    clean = [hint for hint in hints if isinstance(hint, str) and hint.strip()]
                    if clean:
                        _local_hints[game_id] = clean
                        return list(clean)
        except Exception as exc:
            logger.debug("Runtime hints read failed game=%s: %s", game_id, exc)

    return list(_local_hints.get(game_id, []))


async def set_runtime_hints(game_id: str, hints: list[str]) -> None:
    clean = [hint for hint in hints if isinstance(hint, str) and hint.strip()]
    _local_hints[game_id] = clean
    if not redis_client:
        return
    try:
        await redis_client.set(_hints_key(game_id), json.dumps(clean, ensure_ascii=False), ex=_RUNTIME_TTL_S)  # type: ignore[misc]
    except Exception as exc:
        logger.debug("Runtime hints write failed game=%s: %s", game_id, exc)


async def get_runtime_history(game_id: str) -> list[dict]:
    local = _local_history.get(game_id)
    if local:
        return [dict(item) for item in local]

    if redis_client:
        try:
            raw_items = await redis_client.lrange(_history_key(game_id), 0, -1)  # type: ignore[misc]
            if raw_items:
                history: list[dict] = []
                for raw in raw_items:
                    text = _decode_text(raw)
                    if not text:
                        continue
                    item = json.loads(text)
                    if isinstance(item, dict):
                        history.append(item)
                if history:
                    _local_history[game_id] = history
                    return [dict(item) for item in history]
        except Exception as exc:
            logger.debug("Runtime history read failed game=%s: %s", game_id, exc)

    return [dict(item) for item in _local_history.get(game_id, [])]


async def append_runtime_history(game_id: str, item: dict) -> None:
    _local_history.setdefault(game_id, []).append(dict(item))
    if not redis_client:
        return
    try:
        pipe = redis_client.pipeline()
        pipe.rpush(_history_key(game_id), json.dumps(item, ensure_ascii=False))
        pipe.ltrim(_history_key(game_id), -32, -1)
        pipe.expire(_history_key(game_id), _RUNTIME_TTL_S)
        await pipe.execute()
    except Exception as exc:
        logger.debug("Runtime history append failed game=%s: %s", game_id, exc)


async def clear_runtime_state(game_id: str) -> None:
    _local_hints.pop(game_id, None)
    _local_history.pop(game_id, None)
    if redis_client:
        try:
            await redis_client.delete(_hints_key(game_id), _history_key(game_id))  # type: ignore[misc]
        except Exception as exc:
            logger.debug("Runtime state clear failed game=%s: %s", game_id, exc)


class GameEventSubscription:
    def __init__(
        self,
        *,
        game_id: str,
        subscriber_id: str,
        queue: asyncio.Queue | None = None,  # type: ignore[type-arg]
        pubsub=None,
    ) -> None:
        self.game_id = game_id
        self.subscriber_id = subscriber_id
        self._queue = queue
        self._pubsub = pubsub
        self._closed = False

    async def get(self) -> dict:
        if self._queue is not None:
            return await self._queue.get()

        channel = _events_channel(self.game_id)
        while True:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message:
                await asyncio.sleep(0.05)
                continue

            payload_raw = _decode_text(message.get("data"))
            if not payload_raw:
                continue
            try:
                envelope = json.loads(payload_raw)
            except Exception:
                logger.debug("Runtime event decode failed game=%s channel=%s", self.game_id, channel)
                continue
            if not isinstance(envelope, dict):
                continue
            if envelope.get("sender_id") == self.subscriber_id:
                continue
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                return payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._queue is not None:
            subs = _local_subscribers.get(self.game_id)
            if subs:
                subs.pop(self.subscriber_id, None)
                if not subs:
                    _local_subscribers.pop(self.game_id, None)
            return
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(_events_channel(self.game_id))
            except Exception:
                pass
            try:
                await self._pubsub.aclose()
            except Exception:
                pass


async def open_game_event_subscription(game_id: str, subscriber_id: str) -> GameEventSubscription:
    if redis_client:
        try:
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(_events_channel(game_id))
            return GameEventSubscription(game_id=game_id, subscriber_id=subscriber_id, pubsub=pubsub)
        except Exception as exc:
            logger.warning("Runtime pubsub subscribe failed game=%s: %s", game_id, exc)

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)  # type: ignore[type-arg]
    _local_subscribers[game_id][subscriber_id] = queue
    return GameEventSubscription(game_id=game_id, subscriber_id=subscriber_id, queue=queue)


async def publish_runtime_event(game_id: str, payload: dict, *, exclude_subscriber_id: str | None = None) -> None:
    if redis_client:
        try:
            envelope = {"sender_id": exclude_subscriber_id or "", "payload": payload}
            await redis_client.publish(_events_channel(game_id), json.dumps(envelope, ensure_ascii=False))  # type: ignore[misc]
            return
        except Exception as exc:
            logger.warning("Runtime pubsub publish failed game=%s: %s", game_id, exc)

    subs = _local_subscribers.get(game_id, {})
    for subscriber_id, queue in list(subs.items()):
        if subscriber_id == exclude_subscriber_id:
            continue
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("Runtime local queue full game=%s subscriber=%s", game_id, subscriber_id)


@asynccontextmanager
async def game_mutation_lock(game_id: str):
    if redis_client:
        try:
            lock = redis_client.lock(_lock_key(game_id), timeout=15, blocking_timeout=5)
            acquired = await lock.acquire()
            if not acquired:
                raise TimeoutError("lock acquire returned False")
            try:
                yield
                return
            finally:
                try:
                    await lock.release()
                except Exception as exc:
                    logger.debug("Runtime lock release failed game=%s: %s", game_id, exc)
        except Exception as exc:
            logger.warning("Runtime lock fallback to local game=%s: %s", game_id, exc)

    _sweep_game_locks()
    lock = _local_locks.setdefault(game_id, asyncio.Lock())
    async with lock:
        yield


def reset_runtime_state_for_tests() -> None:
    for game_id, subscribers in list(_local_subscribers.items()):
        for queue in list(subscribers.values()):
            while not queue.empty():
                queue.get_nowait()
        _local_subscribers.pop(game_id, None)
    _local_hints.clear()
    _local_history.clear()
    _local_locks.clear()

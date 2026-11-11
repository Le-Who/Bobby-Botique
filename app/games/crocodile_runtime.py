from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from typing import Any

from app.cache import redis_client
from app.utils.json_compat import json

logger = logging.getLogger(__name__)

_RUNTIME_TTL_S = 2 * 24 * 60 * 60
_EVENTS_CHANNEL_PREFIX = "croc:runtime:events:"
_HINTS_KEY_PREFIX = "croc:runtime:hints:"
_HISTORY_KEY_PREFIX = "croc:runtime:history:"
_SEQ_KEY_PREFIX = "croc:runtime:seq:"
_PENDING_KEY_PREFIX = "croc:runtime:pending:"
_LOCK_KEY_PREFIX = "croc:lock:"
_HISTORY_LIMIT = 32
_PENDING_RESULTS_LIMIT = 32
_MUTATION_LEASE_TTL_S = 60.0
_MUTATION_RENEW_INTERVAL_S = 20.0
_MUTATION_WAIT_TIMEOUT_S = 5.0
_RENEW_MUTATION = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
_RELEASE_MUTATION = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class GameMutationLeaseLost(TimeoutError):
    """An admitted mutation no longer has distributed write authority."""


class _MutationLease:
    def __init__(self, client, key: str, token: str, confirmed: float):
        self.client = client
        self.key = key
        self.token = token
        self.confirmed = confirmed
        self.owner = asyncio.current_task()
        self.children: set[asyncio.Task] = set()
        self.lost = False
        self.marker = object()
        self.cancel_sent = False

    def lose(self) -> None:
        if self.lost:
            return
        self.lost = True
        for child in self.children:
            if child is not asyncio.current_task() and not child.done():
                child.cancel(self.marker)
        if self.owner is not None and self.owner is not asyncio.current_task() and not self.owner.done():
            self.cancel_sent = True
            self.owner.cancel(self.marker)

    def check(self) -> None:
        if time.monotonic() >= self.confirmed + _MUTATION_LEASE_TTL_S:
            self.lose()
        if self.lost:
            raise GameMutationLeaseLost("game mutation lease lost")

    async def renew(self) -> None:
        while True:
            try:
                await asyncio.sleep(_MUTATION_RENEW_INTERVAL_S)
                self.check()
                started = time.monotonic()
                remaining = self.confirmed + _MUTATION_LEASE_TTL_S - started
                async with asyncio.timeout(min(5.0, remaining)):
                    renewed = await self.client.eval(
                        _RENEW_MUTATION, 1, self.key, self.token, int(_MUTATION_LEASE_TTL_S * 1000)
                    )
                self.check()
                if renewed != 1:
                    self.lose()
                    return
                self.confirmed = started
            except Exception as exc:
                logger.warning("Game mutation renewal unavailable type=%s", type(exc).__name__)
                self.lose()
                return


_mutation_lease: contextvars.ContextVar[_MutationLease | None] = contextvars.ContextVar(
    "game_mutation_lease", default=None
)


def ensure_game_mutation_owned() -> None:
    lease = _mutation_lease.get()
    if lease is not None:
        lease.check()


async def save_owned_game(game_key: str, payload: bytes, ttl: int) -> bool:
    """Atomically fence classic Redis persistence against lease replacement."""
    if _mutation_lease.get() is None:
        return False
    await _owned_redis_write("redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3]); return 1", [game_key], payload, ttl)
    return True


async def _owned_redis_write(script: str, keys: list[str], *args):
    lease = _mutation_lease.get()
    assert lease is not None
    lease.check()
    fenced = "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return redis.error_reply('GAME_LEASE_LOST') end\n"
    try:
        async with asyncio.timeout(5.0):
            result = await lease.client.eval(fenced + script, len(keys) + 1, lease.key, *keys, lease.token, *args)
    except Exception as exc:
        lease.lose()
        raise GameMutationLeaseLost("game mutation persistence unconfirmed") from exc
    lease.check()
    return result


async def delete_owned_game(game_key: str) -> bool:
    if _mutation_lease.get() is None:
        return False
    await _owned_redis_write("return redis.call('DEL', KEYS[2])", [game_key])
    return True


async def load_owned_game(game_key: str) -> tuple[bool, bytes | str | None]:
    lease = _mutation_lease.get()
    if lease is None:
        return False, None
    lease.check()
    try:
        async with asyncio.timeout(5.0):
            payload = await lease.client.get(game_key)
    except Exception as exc:
        lease.lose()
        raise GameMutationLeaseLost("game mutation source unavailable") from exc
    lease.check()
    return True, payload


_local_hints: dict[str, list[str]] = {}
_local_history: dict[str, list[dict]] = {}
_local_subscribers: dict[str, dict[str, asyncio.Queue]] = defaultdict(dict)  # type: ignore[type-arg]
_local_locks: dict[str, asyncio.Lock] = {}
_local_event_seq: dict[str, int] = {}
_local_pending_results: dict[str, dict[str, dict]] = defaultdict(dict)
_game_locks = _local_locks


async def await_owned_game_action[T](action: Coroutine[Any, Any, T]) -> T:
    """Finish admitted game work before restoring transport cancellation.

    The caller owns its mutation lock. Keeping the child joined here prevents
    a disconnected socket from releasing that lock after persistence but before
    the replay result and required completion work have finished.
    """
    lease = _mutation_lease.get()
    if lease is not None:
        try:
            lease.check()
        except BaseException:
            action.close()
            raise
    accepted_task = asyncio.create_task(action)
    if lease is not None:
        lease.children.add(accepted_task)
    cancellation = None
    while not accepted_task.done():
        try:
            await asyncio.shield(accepted_task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except Exception:
            break
    try:
        result = accepted_task.result()
        ensure_game_mutation_owned()
        return result
    finally:
        if lease is not None:
            lease.children.discard(accepted_task)
        if cancellation is not None and not (lease and lease.lost):
            raise cancellation


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


def _seq_key(game_id: str) -> str:
    return f"{_SEQ_KEY_PREFIX}{game_id}"


def _pending_key(game_id: str) -> str:
    return f"{_PENDING_KEY_PREFIX}{game_id}"


def _lock_key(game_id: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{game_id}"


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
        await redis_client.set(_hints_key(game_id), json.dumps_bytes(clean), ex=_RUNTIME_TTL_S)  # type: ignore[misc]
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


async def get_runtime_replay(game_id: str, *, after_seq: int = 0) -> list[dict]:
    history = await get_runtime_history(game_id)
    if after_seq <= 0:
        return history
    replay: list[dict] = []
    for item in history:
        seq = item.get("seq")
        if isinstance(seq, int) and seq > after_seq:
            replay.append(dict(item))
    return replay


async def _next_event_seq(game_id: str) -> int:
    ensure_game_mutation_owned()
    current = _local_event_seq.get(game_id, 0)
    if _mutation_lease.get() is not None:
        next_seq = int(
            await _owned_redis_write(
                "local seq = redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], ARGV[2]); return seq",
                [_seq_key(game_id)],
                _RUNTIME_TTL_S,
            )
        )
        _local_event_seq[game_id] = max(current, next_seq)
        return next_seq
    if redis_client:
        try:
            next_seq = int(await redis_client.incr(_seq_key(game_id)))  # type: ignore[misc]
            await redis_client.expire(_seq_key(game_id), _RUNTIME_TTL_S)  # type: ignore[misc]
            _local_event_seq[game_id] = max(current, next_seq)
            return next_seq
        except Exception as exc:
            logger.debug("Runtime seq increment failed game=%s: %s", game_id, exc)
    next_seq = current + 1
    _local_event_seq[game_id] = next_seq
    return next_seq


async def stamp_runtime_payload(game_id: str, payload: dict) -> dict:
    stamped = dict(payload)
    seq = stamped.get("seq")
    server_time_ms = stamped.get("server_time_ms")
    if isinstance(seq, int) and isinstance(server_time_ms, int):
        _local_event_seq[game_id] = max(_local_event_seq.get(game_id, 0), seq)
        return stamped
    stamped["seq"] = await _next_event_seq(game_id)
    stamped["server_time_ms"] = int(time.time() * 1000)
    return stamped


async def append_runtime_history(game_id: str, item: dict) -> dict:
    stamped = await stamp_runtime_payload(game_id, item)
    ensure_game_mutation_owned()
    if _mutation_lease.get() is not None:
        await _owned_redis_write(
            "redis.call('RPUSH', KEYS[2], ARGV[2]); redis.call('LTRIM', KEYS[2], -32, -1); "
            "return redis.call('EXPIRE', KEYS[2], ARGV[3])",
            [_history_key(game_id)],
            json.dumps_bytes(stamped),
            _RUNTIME_TTL_S,
        )
    _local_history.setdefault(game_id, []).append(dict(stamped))
    if len(_local_history[game_id]) > _HISTORY_LIMIT:
        _local_history[game_id] = _local_history[game_id][-_HISTORY_LIMIT:]
    if not redis_client or _mutation_lease.get() is not None:
        return stamped
    try:
        pipe = redis_client.pipeline()
        pipe.rpush(_history_key(game_id), json.dumps_bytes(stamped))
        pipe.ltrim(_history_key(game_id), -_HISTORY_LIMIT, -1)
        pipe.expire(_history_key(game_id), _RUNTIME_TTL_S)
        await pipe.execute()
    except Exception as exc:
        logger.debug("Runtime history append failed game=%s: %s", game_id, exc)
    return stamped


async def get_cached_pending_action_result(game_id: str, pending_id: str) -> dict | None:
    if not pending_id:
        return None

    local = _local_pending_results.get(game_id, {})
    cached = local.get(pending_id)
    if isinstance(cached, dict):
        return dict(cached)

    if redis_client:
        try:
            raw = await redis_client.hget(_pending_key(game_id), pending_id)  # type: ignore[misc]
            text = _decode_text(raw)
            if text:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    _local_pending_results[game_id][pending_id] = dict(payload)
                    return dict(payload)
        except Exception as exc:
            logger.debug("Runtime pending result read failed game=%s pending_id=%s: %s", game_id, pending_id, exc)

    return None


async def cache_pending_action_result(game_id: str, pending_id: str, payload: dict) -> None:
    if not pending_id:
        return

    ensure_game_mutation_owned()
    stamped = dict(payload)
    if _mutation_lease.get() is not None:
        await _owned_redis_write(
            "redis.call('HSET', KEYS[2], ARGV[2], ARGV[3]); return redis.call('EXPIRE', KEYS[2], ARGV[4])",
            [_pending_key(game_id)],
            pending_id,
            json.dumps_bytes(stamped),
            _RUNTIME_TTL_S,
        )
    bucket = _local_pending_results.setdefault(game_id, {})
    bucket[pending_id] = stamped
    if len(bucket) > _PENDING_RESULTS_LIMIT:
        oldest = next(iter(bucket))
        bucket.pop(oldest, None)

    if not redis_client or _mutation_lease.get() is not None:
        return
    try:
        pipe = redis_client.pipeline()
        pipe.hset(_pending_key(game_id), mapping={pending_id: json.dumps_bytes(stamped)})
        pipe.expire(_pending_key(game_id), _RUNTIME_TTL_S)
        await pipe.execute()
    except Exception as exc:
        logger.debug("Runtime pending result write failed game=%s pending_id=%s: %s", game_id, pending_id, exc)


async def clear_runtime_state(game_id: str) -> None:
    ensure_game_mutation_owned()
    if _mutation_lease.get() is not None:
        await _owned_redis_write(
            "return redis.call('DEL', KEYS[2], KEYS[3], KEYS[4], KEYS[5])",
            [_hints_key(game_id), _history_key(game_id), _seq_key(game_id), _pending_key(game_id)],
        )
    _local_hints.pop(game_id, None)
    _local_history.pop(game_id, None)
    _local_event_seq.pop(game_id, None)
    _local_pending_results.pop(game_id, None)
    if redis_client and _mutation_lease.get() is None:
        try:
            await redis_client.delete(
                _hints_key(game_id),
                _history_key(game_id),
                _seq_key(game_id),
                _pending_key(game_id),
            )  # type: ignore[misc]
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


async def publish_runtime_event(game_id: str, payload: dict, *, exclude_subscriber_id: str | None = None) -> dict:
    ensure_game_mutation_owned()
    stamped = await stamp_runtime_payload(game_id, payload)
    if _mutation_lease.get() is not None:
        envelope = {"sender_id": exclude_subscriber_id or "", "payload": stamped}
        await _owned_redis_write(
            "return redis.call('PUBLISH', ARGV[2], ARGV[3])", [], _events_channel(game_id), json.dumps_bytes(envelope)
        )
        return stamped
    if redis_client:
        try:
            envelope = {"sender_id": exclude_subscriber_id or "", "payload": stamped}
            await redis_client.publish(_events_channel(game_id), json.dumps_bytes(envelope))  # type: ignore[misc]
            return stamped
        except Exception as exc:
            logger.warning("Runtime pubsub publish failed game=%s: %s", game_id, exc)

    subs = _local_subscribers.get(game_id, {})
    for subscriber_id, queue in list(subs.items()):
        if subscriber_id == exclude_subscriber_id:
            continue
        try:
            queue.put_nowait(stamped)
        except asyncio.QueueFull:
            logger.warning("Runtime local queue full game=%s subscriber=%s", game_id, subscriber_id)
    return stamped


def get_runtime_health_snapshot() -> dict[str, int]:
    return {
        "tracked_games": len(_local_event_seq),
        "history_buffers": len(_local_history),
        "pending_result_buckets": len(_local_pending_results),
        "subscribers": sum(len(items) for items in _local_subscribers.values()),
    }


@asynccontextmanager
async def game_mutation_lock(game_id: str):
    client = redis_client
    if client:
        key, token = _lock_key(game_id), uuid.uuid4().hex
        deadline = time.monotonic() + _MUTATION_WAIT_TIMEOUT_S
        try:
            while True:
                started = time.monotonic()
                async with asyncio.timeout(max(0, deadline - started)):
                    acquired = await client.set(key, token, nx=True, px=int(_MUTATION_LEASE_TTL_S * 1000))
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except Exception as exc:
            raise TimeoutError(f"game_mutation_lock: timed out waiting for game={game_id}") from exc
        lease = _MutationLease(client, key, token, started)
        scope = _mutation_lease.set(lease)
        renewal = asyncio.create_task(lease.renew())
        try:
            lease.check()
            yield
            lease.check()
        except asyncio.CancelledError:
            if lease.lost:
                raise GameMutationLeaseLost("game mutation lease lost") from None
            raise
        finally:

            async def release():
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                try:
                    async with asyncio.timeout(5.0):
                        await client.eval(_RELEASE_MUTATION, 1, key, token)
                except Exception as exc:
                    logger.debug("Runtime lock release failed game=%s type=%s", game_id, type(exc).__name__)

            cleanup = asyncio.create_task(release())
            cancellation = None
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
            cleanup.result()
            _mutation_lease.reset(scope)
            if lease.cancel_sent and lease.owner is not None:
                lease.owner.uncancel()
            if cancellation is not None:
                raise cancellation
        return
    local_lock = _local_locks.setdefault(game_id, asyncio.Lock())
    async with local_lock:
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
    _local_event_seq.clear()
    _local_pending_results.clear()

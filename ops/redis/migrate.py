"""Copy a quiescent Redis DB, preserving opaque values and absolute expirations.

Run in the bot image with SOURCE_REDIS_URL/DESTINATION_REDIS_URL supplied through
a protected env file. The operator must stop every writer first. Never logs keys.
"""

# ruff: noqa: T201 -- counts/errors only; snapshot content never reaches stdout.

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

_SNAPSHOT = (
    "local d=redis.call('DUMP',KEYS[1]); if not d then return nil end; return {d,redis.call('PEXPIRETIME',KEYS[1])}"
)


def server_ms(client: Redis) -> int:
    seconds, micros = client.time()
    return seconds * 1000 + micros // 1000


def value_fingerprint(client: Redis, key: bytes) -> tuple[bytes, str] | None:
    """Compare logical contents: DUMP includes version-dependent serialization."""
    kind = client.type(key)
    if kind == b"none":
        return None
    if kind == b"string":
        value = client.get(key)
    elif kind == b"hash":
        value = sorted(client.hgetall(key).items())
    elif kind == b"set":
        value = sorted(client.smembers(key))
    elif kind == b"zset":
        value = client.zrange(key, 0, -1, withscores=True)
    elif kind == b"list":
        value = client.lrange(key, 0, -1)
    else:
        raise RuntimeError("Migration requires a verifier for this Redis data type")
    return kind, hashlib.sha256(repr(value).encode()).hexdigest()


def copy_verified(source: Redis, destination: Redis, snapshot: Path) -> dict:
    if destination.dbsize():
        raise RuntimeError("Migration destination must be empty")
    if abs(server_ms(source) - server_ms(destination)) > 2000:
        raise RuntimeError("Redis clocks differ by more than two seconds")
    records = []
    seen = set()
    total_bytes = 0
    for key in source.scan_iter(count=100):
        if key in seen:
            continue
        seen.add(key)
        row = source.eval(_SNAPSHOT, 1, key)
        if row is None:
            continue
        payload, expires_at = row
        value_fingerprint(source, key)  # Reject unsupported types before any copy.
        total_bytes += len(payload)
        if len(records) >= 100000 or total_bytes > 256 * 1024 * 1024:
            raise RuntimeError("Migration snapshot exceeds bounded copy capacity")
        records.append(
            {
                "key": base64.b64encode(key).decode(),
                "dump": base64.b64encode(payload).decode(),
                "expires_at": int(expires_at),
            }
        )
    snapshot.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation preserves previous recovery material on reruns.
    with snapshot.open("x", encoding="utf-8") as output:
        json.dump({"version": 1, "records": records}, output, separators=(",", ":"))
    snapshot.chmod(0o600)
    copied = 0
    for row in records:
        key, payload = base64.b64decode(row["key"]), base64.b64decode(row["dump"])
        expires = row["expires_at"]
        if expires >= 0 and expires <= server_ms(destination):
            continue
        destination.restore(key, expires if expires >= 0 else 0, payload, absttl=expires >= 0)
        copied += 1
    # Compare live values/expirations with the source again: writes during the
    # copy cause a failure, never a completed migration marker.
    checked = 0
    for key in source.scan_iter(count=100):
        original = source.eval(_SNAPSHOT, 1, key)
        if original is None:
            continue
        same_value = value_fingerprint(source, key) == value_fingerprint(destination, key)
        same_expiry = int(original[1]) == destination.pexpiretime(key)
        if not (same_value and same_expiry):
            expires = int(original[1])
            if expires >= 0 and expires <= server_ms(destination):
                continue
            raise RuntimeError("Redis migration value or expiration verification failed")
        checked += 1
    for key in destination.scan_iter(count=100):
        if not source.exists(key) and destination.exists(key):
            raise RuntimeError("Redis destination contains unexpected data")
    return {"copied": copied, "verified": checked, "serialized_bytes": total_bytes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        options = {"socket_timeout": 5, "socket_connect_timeout": 5, "retry": Retry(NoBackoff(), 0)}
        with (
            Redis.from_url(os.environ["SOURCE_REDIS_URL"], **options) as source,
            Redis.from_url(os.environ["DESTINATION_REDIS_URL"], **options) as destination,
        ):
            print(json.dumps(copy_verified(source, destination, args.snapshot)))
    except Exception as error:
        print(json.dumps({"migration": "failed", "error_type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

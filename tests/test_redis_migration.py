"""Migration contracts execute against two disposable, otherwise empty Redis DBs."""

import importlib.util
import os
from urllib.parse import urlsplit, urlunsplit

import pytest
from redis import Redis

pytestmark = pytest.mark.integration


def migration():
    spec = importlib.util.spec_from_file_location("redis_migration", "ops/redis/migrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_copy_preserves_binary_values_types_and_absolute_expiry(tmp_path):
    url = os.getenv("TEST_REDIS_URL")
    if not url:
        pytest.skip("Explicit disposable Redis required")
    parts = urlsplit(url)
    assert not parts.query, "Migration test expects a simple disposable URL"
    source = Redis.from_url(urlunsplit(parts._replace(path="/13")))
    destination = Redis.from_url(urlunsplit(parts._replace(path="/14")))
    if source.dbsize() or destination.dbsize():
        pytest.fail("Migration test requires empty disposable DB13/14; refuses to clear existing data")
    try:
        source.set(b"binary\x00key", b"value\x00\xff", px=120000)
        source.hset(b"hash", mapping={b"field": b"secret-test-value"})
        source.zadd(b"leases", {b"owner": 123.5})
        source.sadd(b"members", b"one", b"two")
        source.rpush(b"pending", b"first", b"second")
        expires = source.pexpiretime(b"binary\x00key")
        result = migration().copy_verified(source, destination, tmp_path / "snapshot.json")
        assert result["copied"] == 5
        assert destination.get(b"binary\x00key") == b"value\x00\xff"
        assert destination.pexpiretime(b"binary\x00key") == expires
        assert destination.hgetall(b"hash") == {b"field": b"secret-test-value"}
        assert destination.zrange(b"leases", 0, -1, withscores=True) == [(b"owner", 123.5)]
        assert destination.smembers(b"members") == {b"one", b"two"}
        assert destination.lrange(b"pending", 0, -1) == [b"first", b"second"]
        assert source.dbsize() == 5
        with pytest.raises(RuntimeError, match="empty"):
            migration().copy_verified(source, destination, tmp_path / "second.json")
    finally:
        source.delete(b"binary\x00key", b"hash", b"leases", b"members", b"pending")
        destination.delete(b"binary\x00key", b"hash", b"leases", b"members", b"pending")
        source.close()
        destination.close()

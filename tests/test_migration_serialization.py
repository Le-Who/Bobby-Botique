"""Concurrency regressions for the numbered SQL migration runner."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Transaction(_AsyncContext):
    async def __aenter__(self):
        self.value.transaction_depth += 1
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        self.value.transaction_depth -= 1
        if self.value.transaction_depth == 0 and self.value.owns_migration_lock:
            self.value.owns_migration_lock = False
            self.value.pool.migration_lock.release()
        return False


class _MigrationConnection:
    def __init__(self, pool):
        self.pool = pool
        self.owns_migration_lock = False
        self.transaction_depth = 0

    def transaction(self):
        return _Transaction(self)

    async def execute(self, sql, *args):
        normalized = " ".join(sql.split())
        if "pg_advisory_xact_lock" in normalized:
            await self.pool.migration_lock.acquire()
            self.owns_migration_lock = True
        elif normalized == "SELECT 42;":
            self.pool.migration_executions += 1
            if self.pool.migration_executions == 1:
                self.pool.first_migration_started.set()
                await self.pool.allow_first_migration_to_finish.wait()
        elif "INSERT INTO schema_migrations" in normalized:
            self.pool.applied_versions.add(str(args[0]))
        return "OK"


class _MigrationPool:
    def __init__(self):
        self.migration_lock = asyncio.Lock()
        self.applied_versions: set[str] = set()
        self.migration_executions = 0
        self.first_migration_started = asyncio.Event()
        self.allow_first_migration_to_finish = asyncio.Event()

    def acquire(self):
        return _AsyncContext(_MigrationConnection(self))


@pytest.mark.asyncio
async def test_concurrent_runners_apply_each_numbered_migration_once(monkeypatch, tmp_path):
    """A second app instance must re-read applied versions after serialization."""
    from app.db import migrations

    migration_dir = tmp_path / "scripts" / "migrations"
    migration_dir.mkdir(parents=True)
    (migration_dir / "001_serial.sql").write_text("SELECT 42;", encoding="utf-8")

    fake_module_path = tmp_path / "app" / "db" / "migrations.py"
    monkeypatch.setattr(
        migrations,
        "pathlib",
        SimpleNamespace(Path=lambda _path: fake_module_path),
    )
    monkeypatch.setattr(migrations, "_run_legacy_migrations", AsyncMock())

    pool = _MigrationPool()
    manager = SimpleNamespace(pool=pool)

    async def db_query(sql, params=(), conn=None):
        del conn
        if "SELECT version FROM schema_migrations" in sql:
            return [{"version": version} for version in sorted(pool.applied_versions)]
        return []

    first = asyncio.create_task(migrations.run_migrations(db_query, manager))
    await asyncio.wait_for(pool.first_migration_started.wait(), timeout=1)

    second = asyncio.create_task(migrations.run_migrations(db_query, manager))
    await asyncio.sleep(0)
    pool.allow_first_migration_to_finish.set()

    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.success is True
    assert second_result.success is True
    assert pool.migration_executions == 1
    assert pool.applied_versions == {"001"}


@pytest.mark.asyncio
async def test_numbered_migration_failure_skips_legacy_ddl(monkeypatch):
    """A broken numbered schema must not be mutated further by legacy DDL."""
    from app.db import migrations

    pool = _MigrationPool()
    pool.allow_first_migration_to_finish.set()
    manager = SimpleNamespace(pool=pool)

    async def fail_numbered(_locked_query, _conn, result):
        result.failed.append(("014", "missing role"))

    legacy = AsyncMock()
    monkeypatch.setattr(migrations, "_run_numbered_migrations_locked", fail_numbered)
    monkeypatch.setattr(migrations, "_run_legacy_migrations", legacy)

    result = await migrations.run_migrations(AsyncMock(), manager)

    assert result.success is False
    legacy.assert_not_awaited()

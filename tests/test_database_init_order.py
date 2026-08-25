"""Startup ordering tests for migrations and row-level security."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.errors import DatabasePoolError


@pytest.mark.asyncio
async def test_schema_migrations_run_before_runtime_rls_setup():
    from app.database import _init_schema

    order: list[str] = []

    async def mark(name, result=None):
        order.append(name)
        return result

    async def mark_tables(*_):
        return await mark("tables")

    async def mark_migrations(*_):
        return await mark("migrations", migration_result)

    async def mark_rls():
        return await mark("rls")

    async def mark_seed(*_):
        return await mark("seed")

    migration_result = SimpleNamespace(success=True, applied=[], failed=[], pending_at_start=0)
    with (
        patch("app.db.schema.create_tables", new=AsyncMock(side_effect=mark_tables)),
        patch("app.db.migrations.run_migrations", new=AsyncMock(side_effect=mark_migrations)),
        patch("app.database.setup_row_level_security", new=AsyncMock(side_effect=mark_rls)),
        patch("app.db.seed.insert_initial_data", new=AsyncMock(side_effect=mark_seed)),
    ):
        await _init_schema()

    assert order == ["tables", "migrations", "rls", "seed"]


@pytest.mark.asyncio
async def test_failed_migration_stops_startup_before_rls_and_seed():
    from app.database import _init_schema

    migration_result = SimpleNamespace(
        success=False,
        applied=[],
        failed=[("067", "broken schema")],
        pending_at_start=1,
    )
    with (
        patch("app.db.schema.create_tables", new_callable=AsyncMock),
        patch("app.db.migrations.run_migrations", new_callable=AsyncMock, return_value=migration_result),
        patch("app.database.setup_row_level_security", new_callable=AsyncMock) as rls,
        patch("app.db.seed.insert_initial_data", new_callable=AsyncMock) as seed,
    ):
        with pytest.raises(DatabasePoolError, match="067"):
            await _init_schema()

    rls.assert_not_awaited()
    seed.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_rls_setup_stops_startup_before_seed():
    """Tenant-isolation setup is a hard startup gate after migrations."""
    from app.database import _init_schema

    migration_result = SimpleNamespace(
        success=True,
        applied=["068"],
        failed=[],
        pending_at_start=1,
    )
    with (
        patch("app.db.schema.create_tables", new_callable=AsyncMock),
        patch("app.db.migrations.run_migrations", new_callable=AsyncMock, return_value=migration_result),
        patch(
            "app.database.setup_row_level_security",
            new_callable=AsyncMock,
            side_effect=RuntimeError("RLS policy missing"),
        ),
        patch("app.db.seed.insert_initial_data", new_callable=AsyncMock) as seed,
    ):
        with pytest.raises(RuntimeError, match="RLS policy missing"):
            await _init_schema()

    seed.assert_not_awaited()

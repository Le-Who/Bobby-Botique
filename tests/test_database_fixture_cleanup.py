"""Database test fixtures must own connections even when setup or cleanup fails."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.e2e import conftest as e2e_fixtures
from tests.integration import conftest as integration_fixtures


@pytest.fixture(params=[e2e_fixtures, integration_fixtures], ids=["e2e", "integration"])
def database_fixture(request, monkeypatch):
    transaction = SimpleNamespace(start=AsyncMock(), rollback=AsyncMock())
    connection = SimpleNamespace(
        set_type_codec=AsyncMock(),
        transaction=lambda: transaction,
        execute=AsyncMock(),
        reset=AsyncMock(),
        is_in_transaction=lambda: True,
        close=AsyncMock(),
    )
    monkeypatch.setattr("tests.database_fixtures.asyncpg.connect", AsyncMock(return_value=connection))
    return request.param.db_conn.__wrapped__, connection, transaction


@pytest.mark.parametrize("failed_stage", ["codec", "begin"])
async def test_database_fixture_closes_connection_after_setup_failure(database_fixture, failed_stage):
    fixture, connection, transaction = database_fixture
    operation = connection.set_type_codec if failed_stage == "codec" else transaction.start
    operation.side_effect = RuntimeError("synthetic setup failure")

    generator = fixture("unused-test-url")
    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await anext(generator)

    connection.close.assert_awaited_once()


async def test_database_fixture_uses_migrated_schema_and_rolls_back_owned_transaction(database_fixture):
    fixture, connection, transaction = database_fixture
    generator = fixture("unused-test-url")
    try:
        assert await anext(generator) is connection
    finally:
        await generator.aclose()

    connection.execute.assert_not_awaited()
    transaction.rollback.assert_awaited_once()
    connection.reset.assert_not_awaited()
    connection.close.assert_awaited_once()


async def test_database_fixture_preserves_test_failure_and_still_rolls_back(database_fixture):
    fixture, connection, transaction = database_fixture
    generator = fixture("unused-test-url")
    await anext(generator)

    with pytest.raises(ValueError, match="synthetic test failure"):
        await generator.athrow(ValueError("synthetic test failure"))

    transaction.rollback.assert_awaited_once()
    connection.close.assert_awaited_once()


async def test_database_fixture_reports_rollback_failure_and_closes_connection(database_fixture):
    fixture, connection, transaction = database_fixture
    transaction.rollback.side_effect = RuntimeError("synthetic rollback failure")
    generator = fixture("unused-test-url")
    await anext(generator)

    with pytest.raises(RuntimeError, match="synthetic rollback failure"):
        await generator.aclose()

    connection.close.assert_awaited_once()


async def test_database_fixture_cancellation_rolls_back_and_closes(database_fixture):
    fixture, connection, transaction = database_fixture
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def use_connection():
        generator = fixture("unused-test-url")
        try:
            assert await anext(generator) is connection
            entered.set()
            await blocked.wait()
        finally:
            await generator.aclose()

    task = asyncio.create_task(use_connection())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    transaction.rollback.assert_awaited_once()
    connection.close.assert_awaited_once()

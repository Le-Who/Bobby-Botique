"""
Root conftest – loads .env so that ``app.config.settings`` resolves to a real
Settings object for tests that import the production modules directly.
"""

import asyncio
from pathlib import Path

import pytest
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)


def _quiet_exception_handler(loop, context):
    """Suppress asyncpg 'connection was closed' noise during test teardown.

    asyncpg emits 'Future exception was never retrieved' via
    loop.call_exception_handler when a connection is GC'd with a pending op.
    This is cosmetic and harmless — silence it to keep test output clean.
    """
    exception = context.get("exception")
    if exception:
        exc_name = type(exception).__name__
        if exc_name in ("ConnectionDoesNotExistError", "InterfaceError"):
            return  # silently ignore
    # Fall through to default handler for other exceptions
    loop.default_exception_handler(context)


def pytest_configure(config):
    """Suppress cosmetic warnings from asyncio/asyncpg cleanup."""
    config.addinivalue_line("filterwarnings", "ignore::RuntimeWarning:asyncio")
    # Install quiet exception handler on the default event loop policy
    # This suppresses asyncpg "Future exception was never retrieved" noise
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_quiet_exception_handler)
    except RuntimeError:
        pass  # No running event loop yet — will be set by pytest-asyncio


@pytest.fixture(autouse=True, scope="session")
def _cancel_db_background_tasks():
    """Cancel lingering DatabaseManager background tasks after all tests."""
    yield
    try:
        from app.database import db_manager

        for attr in ("_monitor_task", "_reconnect_task"):
            task = getattr(db_manager, attr, None)
            if task and not task.done():
                task.cancel()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clear_user_state():
    """Clear global user state and locks between tests to prevent pollution."""
    from app.state import USER_STATES

    USER_STATES._states.clear()


# Conditionally register the testcontainers fixture so that its absence
# (no Docker, testcontainers not installed) does not break unit test runs.
# Tests that *explicitly* request `postgres_container` will be skipped by
# the fixture itself when it cannot import testcontainers.
try:
    from tests.fixtures.db_container import postgres_container  # noqa: F401

    __all__ = ["postgres_container"]
except ImportError:
    pass  # testcontainers not installed — postgres_container fixture unavailable

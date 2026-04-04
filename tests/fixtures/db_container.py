"""
Session-scoped PostgreSQL testcontainer fixture for integration tests.

Spins up an ephemeral pgvector Postgres instance, runs the full application
migration chain (create_tables → RLS → run_migrations → seed), and yields the
database URL. Safe to use alongside the Supabase TEST_DATABASE_URL strategy —
they operate independently.

Requirements: Docker Engine running, testcontainers installed.
"""

import asyncio
import os

import pytest


def _require_testcontainers():
    """Import testcontainers with a clear error message if Docker is unavailable."""
    try:
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

        return PostgresContainer
    except ImportError as exc:
        pytest.skip(f"testcontainers not installed — skipping DB container tests: {exc}")


@pytest.fixture(scope="session")
def postgres_container():
    """
    Start a PostgreSQL container with pgvector for the test session.

    Yields the database connection URL. Runs the full schema initialisation
    on startup (identical to the production path) to ensure production-parity.

    Key design decisions:
    - Uses asyncio.run() instead of manual loop management to avoid clobbering
      the pytest-asyncio event loop.
    - Sets DATABASE_URL in os.environ BEFORE db_manager.initialize() so the
      singleton config reads the correct URL at pool creation time.
    """
    PostgresContainer = _require_testcontainers()

    postgres = PostgresContainer("pgvector/pgvector:pg16").with_command(
        "-c max_connections=200"
    )

    with postgres as container:
        # asyncpg expects plain postgresql:// not postgresql+psycopg2://
        db_url = container.get_connection_url().replace("postgresql+psycopg2", "postgresql")

        # Override env FIRST — db_manager reads settings.DATABASE_URL at pool creation
        os.environ["DATABASE_URL"] = db_url

        # Patch the settings singleton in-place if it was already materialised.
        # This prevents a stale DATABASE_URL from being used if config was loaded
        # before the container started.
        try:
            from app.config import config_manager  # noqa: PLC0415

            if getattr(config_manager, "_settings", None):
                config_manager._settings.DATABASE_URL = db_url
        except ImportError:
            pass

        # Run the full production migration chain inside an isolated coroutine.
        # asyncio.run() creates and destroys its own loop without touching the
        # global event loop that pytest-asyncio manages.
        async def _init_schema() -> None:
            from app.database import db_manager  # noqa: PLC0415

            # initialize() calls create_pool() which reads DATABASE_URL from env,
            # then calls _init_schema() → create_tables → RLS → run_migrations → seed.
            await db_manager.initialize()

        try:
            asyncio.run(_init_schema())
        except Exception as exc:
            pytest.fail(f"Failed to initialise testcontainer database schema: {exc}")

        yield db_url

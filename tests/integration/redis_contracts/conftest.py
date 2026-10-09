"""Redis-only contracts do not acquire the parent's PostgreSQL transaction."""

import pytest


@pytest.fixture(autouse=True)
def force_test_db_conn():
    """Override only the unrelated PostgreSQL autouse fixture in this directory."""

"""
Root conftest – loads .env so that ``app.config.settings`` resolves to a real
Settings object for tests that import the production modules directly.
"""

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)


def pytest_configure(config):
    """Suppress cosmetic 'Task was destroyed' warnings from asyncio cleanup."""
    config.addinivalue_line(
        "filterwarnings", "ignore::RuntimeWarning:asyncio"
    )


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



"""
Root conftest – loads .env so that ``app.config.settings`` resolves to a real
Settings object for tests that import the production modules directly.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)

os.environ.setdefault("GEMINI_API_KEYS", "dummy_key_for_tests")


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
    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_quiet_exception_handler)
    except RuntimeError:
        pass  # No running event loop yet — will be set by pytest-asyncio


# ---------------------------------------------------------------------------
# Runtime decontamination: repair stale MagicMock bindings between modules
# ---------------------------------------------------------------------------

# Capture the ONE TRUE settings object right after .env is loaded —
# before any test can mutate os.environ or replace app.config.settings.
try:
    from app.config import settings as _canonical_settings
except Exception:
    _canonical_settings = None  # type: ignore[assignment]


def _propagate_real_settings():
    """Ensure every loaded ``app.*`` module uses the canonical Settings object.

    Handles two contamination modes:
    1. MagicMock bindings left by setup_module/teardown_module sys.modules mutations
    2. Duplicate Settings instances created when app.config is purged from
       sys.modules and reimported with mutated os.environ
    """
    if _canonical_settings is None:
        return

    repaired = []

    # Also restore app.config.settings itself if it drifted
    config_mod = sys.modules.get("app.config")
    if config_mod is not None and not isinstance(config_mod, MagicMock):
        current = getattr(config_mod, "settings", None)
        if current is not _canonical_settings:
            setattr(config_mod, "settings", _canonical_settings)
            repaired.append("app.config")

    for mod_name in list(sys.modules):
        if not mod_name.startswith("app."):
            continue
        mod = sys.modules.get(mod_name)
        if mod is None or isinstance(mod, MagicMock):
            continue
        current_settings = getattr(mod, "settings", _SENTINEL)
        if current_settings is _SENTINEL:
            continue  # module doesn't have a 'settings' attribute
        if current_settings is not _canonical_settings:
            setattr(mod, "settings", _canonical_settings)
            repaired.append(mod_name)

    if repaired:
        import logging

        logging.getLogger(__name__).debug(
            "Decontaminated settings in %d modules: %s",
            len(repaired),
            repaired,
        )


_SENTINEL = object()


@pytest.fixture(autouse=True)
def _decontaminate_settings():
    """Auto-heal stale MagicMock settings bindings between test modules and functions.

    Runs BEFORE the first test in each test and AFTER the last test,
    ensuring that any sys.modules mutations
    don't leak MagicMock references into subsequent tests.
    """
    _propagate_real_settings()
    yield
    _propagate_real_settings()


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


@pytest.fixture(autouse=True)
def _clear_game_mem_stores():
    """Clear all Crocodile game in-memory stores between tests.

    _mem_games, _mem_hints, and _mem_history are module-level dicts.
    Without this fixture tests that create games pollute subsequent tests.
    """
    import app.games.crocodile as _croc
    from app.games.crocodile_runtime import reset_runtime_state_for_tests
    from app.games.crocodile_telegram import reset_telegram_state_for_tests

    _croc._mem_games.clear()
    _croc._mem_hints.clear()
    _croc._mem_history.clear()
    reset_runtime_state_for_tests()
    reset_telegram_state_for_tests()
    yield
    _croc._mem_games.clear()
    _croc._mem_hints.clear()
    _croc._mem_history.clear()
    reset_runtime_state_for_tests()
    reset_telegram_state_for_tests()


@pytest.fixture(autouse=True)
def _clear_word_bank_caches():
    """Clear custom-category word caches between tests."""
    import app.games.judgement_cache as _game_cache
    import app.games.word_bank as _word_bank

    for task in list(_word_bank._GENERATED_INFLIGHT.values()):
        if not task.done():
            task.cancel()
    _word_bank._GENERATED_CACHE.clear()
    _word_bank._GENERATED_INFLIGHT.clear()
    _word_bank._PROVISIONAL_GENERATED.clear()
    _word_bank._TOPIC_ROTATION.clear()
    _game_cache._generated_words_store.clear()
    _game_cache._cat_store.clear()
    yield
    for task in list(_word_bank._GENERATED_INFLIGHT.values()):
        if not task.done():
            task.cancel()
    _word_bank._GENERATED_CACHE.clear()
    _word_bank._GENERATED_INFLIGHT.clear()
    _word_bank._PROVISIONAL_GENERATED.clear()
    _word_bank._TOPIC_ROTATION.clear()
    _game_cache._generated_words_store.clear()
    _game_cache._cat_store.clear()


@pytest.fixture(autouse=True)
def _clear_global_caches():
    """Clear all global caches to prevent state leakage between tests.

    This includes ProviderRouter active model caches and DatabaseManager
    key/config caches which may hold stale references.
    """
    from app.config import _invalidate_primary_provider_cache
    from app.database import db_manager

    def _clear():
        _invalidate_primary_provider_cache()
        if hasattr(db_manager, "_active_keys_cache"):
            db_manager._active_keys_cache.clear()
        if hasattr(db_manager, "_model_config_cache"):
            db_manager._model_config_cache.clear()

    _clear()
    yield
    _clear()


# Conditionally register the testcontainers fixture so that its absence
# (no Docker, testcontainers not installed) does not break unit test runs.
# Tests that *explicitly* request `postgres_container` will be skipped by
# the fixture itself when it cannot import testcontainers.
try:
    from tests.fixtures.db_container import postgres_container  # noqa: F401

    __all__ = ["postgres_container"]
except ImportError:
    pass  # testcontainers not installed — postgres_container fixture unavailable

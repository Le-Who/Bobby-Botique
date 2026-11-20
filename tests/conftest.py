"""
Root conftest — installs deterministic non-production settings before any app
module is imported. The production ``.env`` must never enter a unit-test process.
"""

import asyncio
import os

import pytest

_SAFE_TEST_ENV = {
    "TESTING": "true",
    "TELEGRAM_BOT_TOKEN": "1234567890:dummy-token-for-tests-only",
    "ADMIN_SECRET": "dummy-admin-secret-for-tests-only",
    "ADMIN_ID": "1",
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/testdb",
    "REDIS_URL": "",
    "WEBHOOK_URL": "",
    "TELEGRAM_LOCAL_SERVER_URL": "",
    "GEMINI_API_KEYS": "dummy-gemini-key-for-tests",
    "GEMINI_TTS_RPM_PER_KEY": "3",
    "TAVILY_API_KEYS": "dummy-tavily-key-for-tests",
    "OPENROUTER_API_KEYS": "dummy-openrouter-key-for-tests",
    "OPENAI_API_KEYS": "",
    "OPENAI_API_KEY": "",
    "OPENCODE_API_KEYS": "dummy-opencode-key-for-tests",
    "ELEVENLABS_API_KEYS": "",
    "FREETHEAI_API_KEYS": "",
    "POLLINATIONS_API_KEY": "",
    "JINA_API_KEY": "",
    "WEATHER_API_KEY": "",
    "EXCHANGE_RATE_API_KEY": "",
    "VERTEX_AI_KEY": "",
    "NATAL_GEOCODER_PROVIDER": "local",
}
_original_database_url = os.environ.get("DATABASE_URL", "")
if _original_database_url:
    os.environ["GEMAIBOT_TEST_ORIGINAL_DATABASE_URL"] = _original_database_url
os.environ.update(_SAFE_TEST_ENV)


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
    USER_STATES._live_states.clear()


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
    from app.middleware import dedup
    from app.providers.tts_quota import tts_minute_quota

    def _clear():
        _invalidate_primary_provider_cache()
        tts_minute_quota._local.clear()
        dedup._recent_requests.clear()
        dedup._recent_voice_ids.clear()
        if hasattr(db_manager, "_active_keys_cache"):
            db_manager._active_keys_cache.clear()
        if hasattr(db_manager, "_model_config_cache"):
            db_manager._model_config_cache.clear()

    _clear()
    yield
    _clear()

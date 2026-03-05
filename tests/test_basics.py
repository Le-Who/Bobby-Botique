"""Basic sanity tests — verify core modules import and initialize correctly."""

import pytest

from app.config import get_settings_safe


def test_settings_loads_successfully():
    """get_settings_safe() must return a non-None Settings object when .env is present."""
    settings = get_settings_safe()
    assert settings is not None, "Settings should load from .env in test environment"


def test_settings_has_required_fields():
    """Settings object must expose the key configuration attributes."""
    settings = get_settings_safe()
    assert settings is not None
    assert hasattr(settings, "TELEGRAM_BOT_TOKEN")
    assert hasattr(settings, "ADMIN_ID")
    assert hasattr(settings, "AVAILABLE_MODELS")
    assert isinstance(settings.AVAILABLE_MODELS, list)
    assert len(settings.AVAILABLE_MODELS) > 0, "At least one model must be configured"


def test_handler_modules_have_register():
    """Handler modules must expose a register() function."""
    from app.handlers import commands, messages

    assert callable(getattr(commands, "register", None)), "commands.register must be callable"
    assert callable(getattr(messages, "register", None)), "messages.register must be callable"


def test_state_module_provides_user_state():
    """app.state must provide get_user_state that returns a UserState with a lock."""
    import asyncio

    from app.state import get_user_state

    state = get_user_state(99999)
    assert state is not None
    assert hasattr(state, "lock")
    assert isinstance(state.lock, asyncio.Lock)

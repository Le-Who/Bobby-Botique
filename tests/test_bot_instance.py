"""Tests for the bot_instance singleton module."""

import pytest
from unittest.mock import MagicMock

from app.bot_instance import get_bot, register_bot
import app.bot_instance

@pytest.fixture(autouse=True)
def reset_bot_instance():
    """Ensure the bot instance is reset before and after each test."""
    original_bot = app.bot_instance._bot
    app.bot_instance._bot = None
    yield
    app.bot_instance._bot = original_bot

def test_get_bot_initial_state():
    """Test that get_bot returns None initially."""
    assert get_bot() is None

def test_register_and_get_bot():
    """Test registering a mock bot and retrieving it."""
    mock_bot = MagicMock()
    register_bot(mock_bot)
    assert get_bot() is mock_bot

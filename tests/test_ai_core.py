"""Tests for AI core orchestration."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update
from telegram.ext import ContextTypes

from app.handlers.ai_core import handle_ai_query


@pytest.mark.asyncio
async def test_handle_ai_query_empty_prompt():
    """Test that handle_ai_query returns immediately when prompt is empty or whitespace."""
    update = MagicMock(spec=Update)
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    # Test with empty string
    result1 = await handle_ai_query(update, context, "")
    assert result1 is None

    # Test with whitespace string
    result2 = await handle_ai_query(update, context, "   ")
    assert result2 is None

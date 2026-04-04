import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from telegram import Bot, Chat, Message, User

from app.cache import (
    get_long_message,
    get_telegraph_url,
    redis_client,
    store_long_message,
    store_telegraph_url,
)
from app.streaming import stream_and_display


@pytest.mark.asyncio
async def test_store_and_get_long_message():
    """Unit-тест: store_long_message -> get_long_message round-trip"""
    if not redis_client:
        pytest.skip("Redis is not configured")
        
    uid = str(uuid.uuid4())
    test_markdown = "# Test Markdown\n\nThis is a test message."
    
    await store_long_message(uid, test_markdown, ttl=60)
    result = await get_long_message(uid)
    
    assert result == test_markdown
    
    # Test expiration (simulated by key deletion)
    await redis_client.delete(f"long_msg:{uid}")
    empty_result = await get_long_message(uid)
    assert empty_result is None

@pytest.mark.asyncio
async def test_store_and_get_telegraph_fallback():
    """Unit-тест: fallback на Telegraph URL при отсутствии Redis ключа"""
    if not redis_client:
        pytest.skip("Redis is not configured")
        
    uid = str(uuid.uuid4())
    test_url = "https://telegra.ph/Test-01"
    
    await store_telegraph_url(uid, test_url)
    result = await get_telegraph_url(uid)
    
    assert result == test_url
    
@pytest.mark.asyncio
@patch("app.utils.telegraph.httpx.AsyncClient")
async def test_create_telegraph_page(mock_client_class):
    """Тестируем создание fallback-страницы."""
    from app.utils.telegraph import create_telegraph_page
    
    # Mocking the htppx responses
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    
    # First response: createAccount
    response_account = AsyncMock()
    response_account.json.return_value = {"ok": True, "result": {"access_token": "test_token"}}
    response_account.raise_for_status.return_value = None
    
    # Second response: createPage
    response_page = AsyncMock()
    response_page.json.return_value = {"ok": True, "result": {"url": "https://telegra.ph/Mock-Page"}}
    response_page.raise_for_status.return_value = None
    
    # The client will be called differently depending on caching of _access_token 
    # but we will just make it return our mocks sequentially
    mock_client.post.side_effect = [response_account, response_page]
    
    url = await create_telegraph_page("Title", "Markdown text")
    assert url == "https://telegra.ph/Mock-Page"

import pytest
import asyncio
from app.web import _safe_fetch

@pytest.mark.asyncio
async def test_safe_fetch_success():
    async def success_coro():
        return "success_data"

    name, result = await _safe_fetch("test_success", success_coro())
    assert name == "test_success"
    assert result == "success_data"

@pytest.mark.asyncio
async def test_safe_fetch_exception():
    async def error_coro():
        raise ValueError("Something went wrong")

    name, result = await _safe_fetch("test_error", error_coro())
    assert name == "test_error"
    assert isinstance(result, dict)
    assert result["error"] == "Something went wrong"

@pytest.mark.asyncio
async def test_safe_fetch_timeout():
    async def timeout_coro():
        raise TimeoutError("Connection timed out")

    name, result = await _safe_fetch("test_timeout", timeout_coro())
    assert name == "test_timeout"
    assert isinstance(result, dict)
    assert result["error"] == "Connection timed out"

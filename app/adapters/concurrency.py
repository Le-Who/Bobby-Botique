import asyncio

from app.config import settings


class GlobalLLMSemaphore:
    """
    Abstracted global rate limiter / semaphore.
    Currently backed by asyncio.Semaphore in-process.
    Ready for easy transparent swap to Redis distributed lock if scaling is needed.
    """
    def __init__(self, limit: int):
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)

    async def __aenter__(self):
        return await self._semaphore.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self._semaphore.__aexit__(exc_type, exc_val, exc_tb)

_HEAVY_REQUEST_LIMIT = max(1, settings.MAX_CONCURRENT_HEAVY_REQUESTS)
heavy_request_semaphore = GlobalLLMSemaphore(_HEAVY_REQUEST_LIMIT)

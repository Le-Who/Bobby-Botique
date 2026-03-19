import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

MAX_PAGE_CHARS = 15_000  # ~5K tokens

logger = logging.getLogger(__name__)

# Reusable client for connection pooling
_client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)


async def close() -> None:
    """Gracefully close the module-level httpx client (call on shutdown)."""
    await _client.aclose()


@retry(
    wait=wait_exponential(multiplier=1, max=4),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    reraise=True,
)
async def _fetch_jina(url: str, timeout: float) -> str:
    headers = {"Accept": "text/markdown", "X-No-Cache": "true"}
    if settings.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"

    jina_url = f"https://r.jina.ai/{url}"

    response = await _client.get(jina_url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


async def read_url(url: str, timeout: float = 10.0) -> str:
    """
    Fetch clean Markdown from a URL via Jina Reader API.
    Returns error string on failure (never raises).
    Truncates content to MAX_PAGE_CHARS.
    """
    try:
        content = await _fetch_jina(url, timeout)

        if len(content) > MAX_PAGE_CHARS:
            content = content[:MAX_PAGE_CHARS] + "\n\n[...truncated due to length]"

        return content

    except httpx.TimeoutException:
        logger.warning(f"Timeout fetching URL {url} via Jina Reader")
        return f"[Error: Timeout reading URL {url}]"
    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP Error {e.response.status_code} fetching URL {url} via Jina Reader")
        return f"[Error: HTTP {e.response.status_code} reading URL {url}]"
    except Exception as e:
        logger.warning(f"Error fetching URL {url} via Jina Reader: {e}")
        return f"[Error: Failed to read URL {url} - {str(e)}]"

"""Telegraph (Telegra.ph) integration for longread articles.

When a response exceeds TELEGRAPH_THRESHOLD chars, we create a Telegraph page
and send users a collapsed blockquote summary with an Instant View link.

The Telegraph API is free, requires no authentication for page creation,
and Telegram natively renders Instant View for telegra.ph links.

Reference: https://telegra.ph/api
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Responses longer than this threshold get published to Telegraph
TELEGRAPH_THRESHOLD = 5000

# Telegraph API endpoint
_TELEGRAPH_API = "https://api.telegra.ph"

# Cached access_token (created lazily on first use)
_access_token: str | None = None


async def _ensure_account() -> str:
    """Create a Telegraph account (or reuse cached token).

    Returns the access_token for subsequent createPage calls.
    """
    global _access_token
    if _access_token:
        return _access_token

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_TELEGRAPH_API}/createAccount",
            json={
                "short_name": "GemAI Bot",
                "author_name": "GemAI Bot",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegraph createAccount failed: {data}")

    _access_token = data["result"]["access_token"]
    assert isinstance(_access_token, str)
    logger.info("Telegraph account created (token=%s...)", _access_token[:8])
    return _access_token


def _markdown_to_telegraph_nodes(text: str) -> list[dict]:
    """Convert markdown text to Telegraph Node format.

    This is a simplified converter that handles:
    - Paragraphs (double newline separated)
    - Bold (**text**)
    - Italic (*text* or _text_)
    - Code blocks (```code```)
    - Inline code (`code`)
    - Headers (## text)

    For complex markdown, we fall back to plain text paragraphs.
    """
    import re

    nodes: list[dict] = []

    # Split into paragraphs
    paragraphs = text.split("\n\n")

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Headers
        header_match = re.match(r"^(#{1,3})\s+(.+)$", para, re.MULTILINE)
        if header_match:
            level = len(header_match.group(1))
            tag = f"h{min(level + 2, 4)}"  # h3 or h4 for Telegraph
            nodes.append({"tag": tag, "children": [header_match.group(2)]})
            continue

        # Code blocks
        code_match = re.match(r"^```\w*\n(.*?)```$", para, re.DOTALL)
        if code_match:
            nodes.append(
                {
                    "tag": "pre",
                    "children": [code_match.group(1).strip()],
                }
            )
            continue

        # Regular paragraph — apply inline formatting
        formatted = para
        # Bold
        formatted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", formatted)
        # Italic (but not inside bold)
        formatted = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i>\1</i>", formatted)
        formatted = re.sub(r"_(.+?)_", r"<i>\1</i>", formatted)
        # Inline code
        formatted = re.sub(r"`([^`]+)`", r"<code>\1</code>", formatted)

        # Split on newlines within paragraph for line breaks
        lines = formatted.split("\n")
        children: list[str | dict] = []
        for i, line in enumerate(lines):
            children.append(line)
            if i < len(lines) - 1:
                children.append({"tag": "br"})

        nodes.append({"tag": "p", "children": children})

    return nodes


async def create_telegraph_page(title: str, markdown_content: str) -> str | None:
    """Create a Telegraph page from markdown content.

    Args:
        title: Page title (shown in Instant View header).
        markdown_content: The full response text in markdown.

    Returns:
        The Telegraph URL (e.g. https://telegra.ph/My-Article-04-03)
        or None if creation failed.
    """
    try:
        token = await _ensure_account()
        nodes = _markdown_to_telegraph_nodes(markdown_content)

        if not nodes:
            return None

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_TELEGRAPH_API}/createPage",
                json={
                    "access_token": token,
                    "title": title[:256],
                    "author_name": "GemAI Bot",
                    "content": nodes,
                    "return_content": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        if not data.get("ok"):
            logger.warning("Telegraph createPage failed: %s", data)
            return None

        url = data["result"]["url"]
        logger.info("Telegraph page created: %s (%d chars)", url, len(markdown_content))
        return url

    except Exception as e:
        logger.warning("Telegraph page creation failed: %s", e)
        return None


def should_use_telegraph(text: str) -> bool:
    """Check if a response is long enough to warrant a Telegraph page."""
    return len(text) > TELEGRAPH_THRESHOLD

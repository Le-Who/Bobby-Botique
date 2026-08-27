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
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Telegraph API endpoint
_TELEGRAPH_API = "https://api.telegra.ph"

# Cached access_token (created lazily on first use)
_access_token: str | None = None


def is_safe_telegraph_url(value: str | None) -> bool:
    """Accept only canonical HTTPS pages hosted by Telegraph itself."""
    if not value:
        return False
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except TypeError, ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname == "telegra.ph"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
    )


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
        raise RuntimeError("Telegraph createAccount failed")

    _access_token = data["result"]["access_token"]
    assert isinstance(_access_token, str)
    logger.info("Telegraph account created")
    return _access_token


import html
import re
from html.parser import HTMLParser

from app.utils.text_format import markdown_to_html


class TelegraphHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes: list[dict] = []
        self.stack: list[dict] = [{"children": self.nodes}]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: dict = {"tag": tag.lower()}

        # Valid Telegraph tags: a, aside, b, blockquote, br, code, em, figcaption, figure, h3, h4, hr, i, iframe, img, li, ol, p, pre, s, strong, u, ul, video
        if attrs:
            valid_attrs = {}
            for k, v in attrs:
                if v is not None and k in ("href", "src", "class"):
                    valid_attrs[k] = v
            if valid_attrs:
                node["attrs"] = valid_attrs

        if tag.lower() not in ("br", "hr", "img"):
            node["children"] = []
            self.stack[-1].setdefault("children", []).append(node)
            self.stack.append(node)
        else:
            self.stack[-1].setdefault("children", []).append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        # Find matching tag up the stack
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].get("tag") == tag:
                # Pop all up to this tag
                while len(self.stack) > i:
                    self.stack.pop()
                break

    def handle_data(self, data: str) -> None:
        data = html.unescape(data)
        lines = data.split("\n")
        for i, line in enumerate(lines):
            if line:
                self.stack[-1].setdefault("children", []).append(line)
            if i < len(lines) - 1:
                self.stack[-1].setdefault("children", []).append({"tag": "br"})


def _markdown_to_telegraph_nodes(text: str) -> list[dict]:
    """Convert markdown text to Telegraph Node format utilizing standard formatting."""
    # Convert to standard Telegram HTML which accurately parses code blocks and escapes
    html_out = markdown_to_html(text)

    # Split by block-level elements before paragraph wrapping
    segments = re.split(r"(<pre(?:>| [^>]*>).*?</pre>|<blockquote>.*?</blockquote>)", html_out, flags=re.DOTALL)
    final_nodes: list[dict] = []

    for segment in segments:
        if (segment.startswith("<pre") and segment.endswith("</pre>")) or (
            segment.startswith("<blockquote") and segment.endswith("</blockquote>")
        ):
            parser = TelegraphHTMLParser()
            parser.feed(segment)
            final_nodes.extend(parser.nodes)
        else:
            # Wrap standard text splits in paragraphs
            paras = segment.split("\n\n")
            for para in paras:
                para = para.strip()
                if not para:
                    continue
                # If the markdown text produced top-level <b>...</b> we can just wrap in <p>
                parser = TelegraphHTMLParser()
                parser.feed(para)
                if parser.nodes:
                    final_nodes.append({"tag": "p", "children": parser.nodes})

    return final_nodes


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
            logger.warning("Telegraph createPage failed")
            return None

        url = data["result"]["url"]
        if not is_safe_telegraph_url(url):
            logger.warning("Telegraph createPage returned an invalid URL")
            return None
        logger.info("Telegraph page created: %s (%d chars)", url, len(markdown_content))
        return url

    except Exception as e:
        logger.warning("Telegraph page creation failed (error_type=%s)", type(e).__name__)
        return None


async def create_telegraph_page_from_markdown(title: str, markdown_content: str) -> str | None:
    return await create_telegraph_page(title, markdown_content)

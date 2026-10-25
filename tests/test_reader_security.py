"""Reader content remains data at the HTML and Markdown link boundaries."""

import json
import re
from html.parser import HTMLParser
from unittest.mock import AsyncMock

import pytest

from app.utils.reader_utils import markdown_to_reader_html


class _ReaderElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))
        elif tag == "script":
            self.script_count += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["redis", "telegraph"])
async def test_reader_heading_cannot_end_inline_script(monkeypatch, source):
    from app.web import quart_app

    heading = '</script><script data-reader-injected="true">window.readerInjected = true</script>'
    markdown = f"# {heading}\n\n## Обычный раздел & details"
    monkeypatch.setattr("app.cache.get_long_message", AsyncMock(return_value=markdown if source == "redis" else None))
    monkeypatch.setattr("app.cache.get_telegraph_url", AsyncMock(return_value="https://telegra.ph/Synthetic-01"))
    monkeypatch.setattr("app.web_miniapp._fetch_telegraph_content", AsyncMock(return_value=markdown))

    response = await quart_app.test_client().get("/webapp/reader?id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert response.status_code == 200
    document = await response.get_data(as_text=True)
    assert "</script><script" not in document
    serialized = re.search(r"const TOC_DATA\s*=\s*(.*);", document).group(1)
    entries = json.loads(serialized)
    assert [entry["text"] for entry in entries] == [heading, "Обычный раздел & details"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:readerInjected=1",
        "JaVaScRiPt:readerInjected=1",
        "java\tscript:readerInjected=1",
        "data:text/html,synthetic",
        "vbscript:synthetic",
    ],
)
def test_reader_drops_active_markdown_link_schemes(url):
    rendered = markdown_to_reader_html(f"Read [**Synthetic label**]({url}) safely.", [])
    elements = _ReaderElements()
    elements.feed(rendered)
    assert elements.links == []
    assert "<b>Synthetic label</b>" in rendered


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/a?one=1&two=2",
        "http://example.test/",
        "mailto:reader@example.test",
        "/webapp/reader?id=synthetic",
        "#details",
    ],
)
def test_reader_preserves_safe_markdown_link_destinations(url):
    rendered = markdown_to_reader_html(f"Read [Synthetic label]({url}).", [])
    elements = _ReaderElements()
    elements.feed(rendered)
    assert len(elements.links) == 1
    assert elements.links[0]["href"] == url

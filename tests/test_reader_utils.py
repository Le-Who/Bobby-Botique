"""Tests for app.utils.reader_utils — SSR helper utilities.

AAA pattern throughout.  No network calls, no I/O, deterministic.
"""

from app.utils.reader_utils import (
    apply_bionic_reading,
    extract_text_from_telegraph_html,
    extract_toc,
    markdown_to_reader_html,
)

# ── extract_toc ──────────────────────────────────────────────────────────────


def test_extract_toc_returns_entries_for_two_or_more_headings():
    """TOC is built when at least 2 headings are present."""
    md = "# Title\n\nSome text.\n\n## Section One\n\nMore text.\n\n## Section Two\n\nContent."
    toc = extract_toc(md)
    assert len(toc) == 3
    assert toc[0]["level"] == "h1"
    assert toc[0]["text"] == "Title"
    assert toc[1]["level"] == "h2"
    assert toc[1]["text"] == "Section One"


def test_extract_toc_returns_empty_for_single_heading():
    """A single heading produces no TOC (not useful)."""
    md = "# Only Heading\n\nSome content but no other headings."
    toc = extract_toc(md)
    assert toc == []


def test_extract_toc_skips_headings_inside_fenced_code():
    """Headings inside code blocks are NOT included in the TOC."""
    md = (
        "# Real Heading\n\n"
        "```python\n# This is a comment, not a heading\n```\n\n"
        "## Real Section\n"
    )
    toc = extract_toc(md)
    texts = [e["text"] for e in toc]
    assert "Real Heading" in texts
    assert "Real Section" in texts
    assert "This is a comment, not a heading" not in texts


def test_extract_toc_generates_slugs():
    """Anchors are URL-safe slugs derived from heading text."""
    md = "# Hello World!\n\n## Section — Two\n"
    toc = extract_toc(md)
    # Should have 2 entries
    assert len(toc) == 2
    # Anchors must be slug-safe (no spaces, no ! etc.)
    for entry in toc:
        assert " " not in entry["anchor"]
        assert "!" not in entry["anchor"]


def test_extract_toc_deduplicates_anchors():
    """Identical heading text gets distinct anchors via counter suffix."""
    md = "# Item\n\n## Item\n\n### Item\n"
    toc = extract_toc(md)
    anchors = [e["anchor"] for e in toc]
    # All anchors must be unique
    assert len(anchors) == len(set(anchors))


# ── markdown_to_reader_html ───────────────────────────────────────────────────


def test_markdown_to_reader_html_renders_headings_with_anchors():
    """H1/H2/H3 headings get id attributes matching slug anchors."""
    md = "# Hello\n\n## World"
    toc = extract_toc(md)
    out = markdown_to_reader_html(md, toc)
    assert '<h1 id="hello">' in out
    assert '<h2 id="world">' in out


def test_markdown_to_reader_html_renders_fenced_code_blocks():
    """Fenced code blocks produce .code-block wrappers with the correct lang."""
    md = "```python\nprint('hi')\n```"
    out = markdown_to_reader_html(md, [])
    assert 'data-lang="python"' in out
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out


def test_markdown_to_reader_html_escapes_html_in_code():
    """HTML characters inside code blocks are escaped."""
    md = "```html\n<script>alert(1)</script>\n```"
    out = markdown_to_reader_html(md, [])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_markdown_to_reader_html_renders_blockquote():
    """Blockquotes produce <blockquote> tags."""
    md = "> This is a quote."
    out = markdown_to_reader_html(md, [])
    assert "<blockquote>" in out
    assert "This is a quote." in out


def test_markdown_to_reader_html_renders_unordered_list():
    """Unordered lists produce <ul><li> structure."""
    md = "- Alpha\n- Beta\n- Gamma"
    out = markdown_to_reader_html(md, [])
    assert "<ul>" in out
    assert out.count("<li>") == 3


def test_markdown_to_reader_html_renders_ordered_list():
    """Ordered lists produce <ol><li> structure."""
    md = "1. First\n2. Second\n3. Third"
    out = markdown_to_reader_html(md, [])
    assert "<ol>" in out
    assert out.count("<li>") == 3


def test_markdown_to_reader_html_renders_bold_and_italic():
    """Bold (**) and italic (*) inline markup is converted."""
    md = "This is **bold** and *italic*."
    out = markdown_to_reader_html(md, [])
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out


def test_markdown_to_reader_html_renders_links():
    """[text](url) links produce <a href=...> tags."""
    md = "Visit [OpenAI](https://openai.com) for more."
    out = markdown_to_reader_html(md, [])
    assert 'href="https://openai.com"' in out
    assert ">OpenAI<" in out


def test_markdown_to_reader_html_renders_hr():
    """--- produces a <hr> tag."""
    md = "Section A\n\n---\n\nSection B"
    out = markdown_to_reader_html(md, [])
    assert "<hr>" in out


# ── apply_bionic_reading ─────────────────────────────────────────────────────


def test_apply_bionic_reading_boldens_word_stems():
    """Plain HTML text nodes get leading characters wrapped in <b>."""
    html_in = "<p>Hello World</p>"
    out = apply_bionic_reading(html_in)
    # "Hello" has length 5 → k=2 → <b>He</b>llo
    assert "<b>He</b>llo" in out
    # "World" has length 5 → k=2 → <b>Wo</b>rld
    assert "<b>Wo</b>rld" in out


def test_apply_bionic_reading_skips_code_elements():
    """Text inside <code> tags is NOT transformed."""
    html_in = "<p>Text</p><code>some_code here</code>"
    out = apply_bionic_reading(html_in)
    # The code tag contents should be unchanged
    assert "some_code here" in out
    # The paragraph should be transformed
    assert "<b>Te</b>xt" in out


def test_apply_bionic_reading_skips_pre_elements():
    """Text inside <pre><code> blocks is NOT transformed."""
    html_in = "<pre><code>def foo(): pass</code></pre>"
    out = apply_bionic_reading(html_in)
    # Content unchanged
    assert "def foo(): pass" in out
    assert "<b>" not in out


def test_apply_bionic_reading_skips_existing_bold():
    """Text already inside <b> tags is not double-bolded."""
    html_in = "<p><b>Bold</b> text</p>"
    out = apply_bionic_reading(html_in)
    # The word inside <b> is skipped; surrounding text is transformed
    assert "<b>te</b>xt" in out


def test_apply_bionic_reading_word_length_boundaries():
    """Bolding fraction scales correctly with word length."""
    # 1-char: 1 char bold
    assert "<b>I</b>" in apply_bionic_reading("<p>I</p>")
    # 4-char: 2 chars bold
    assert "<b>Te</b>st" in apply_bionic_reading("<p>Test</p>")
    # 7-char: 3 chars bold
    assert "<b>Tab</b>lets" in apply_bionic_reading("<p>Tablets</p>")
    # 10-char: 4 chars bold
    assert "<b>Desc</b>ription" in apply_bionic_reading("<p>Description</p>")


# ── extract_text_from_telegraph_html ─────────────────────────────────────────


def test_extract_text_strips_html_tags():
    """HTML tags are removed from Telegraph article HTML."""
    raw = "<h3>Title</h3><p>Some text here.</p>"
    out = extract_text_from_telegraph_html(raw)
    assert "<" not in out
    assert "Title" in out
    assert "Some text here." in out


def test_extract_text_decodes_html_entities():
    """HTML entities (like &amp; &lt;) are decoded."""
    raw = "<p>5 &gt; 3 &amp; 2 &lt; 4</p>"
    out = extract_text_from_telegraph_html(raw)
    assert "&amp;" not in out
    assert "5 > 3 & 2 < 4" in out


def test_extract_text_normalises_whitespace():
    """Multiple blank lines are collapsed to a single blank line."""
    raw = "<p>A</p>\n\n\n\n<p>B</p>"
    out = extract_text_from_telegraph_html(raw)
    assert "\n\n\n" not in out


def test_extract_text_handles_empty_input():
    """Empty or whitespace input returns an empty string."""
    assert extract_text_from_telegraph_html("") == ""
    assert extract_text_from_telegraph_html("   ") == ""

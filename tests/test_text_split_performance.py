import time

from app.utils.text_format import split_text_safe


def test_split_text_safe_preserves_attributes_on_split():
    """
    Verifies that when a code block with attributes is split,
    the attributes are preserved in subsequent chunks.
    """
    code = "line\n" * 50
    text = f'<pre><code class="language-python">{code}</code></pre>'

    chunks = split_text_safe(text, max_length=60)

    assert len(chunks) > 1

    assert chunks[0].startswith('<pre><code class="language-python">')
    assert chunks[0].endswith("</code></pre>")

    assert chunks[1].startswith('<pre><code class="language-python">')


def test_split_text_safe_performance():
    """
    Simple performance smoke test.
    Ensures splitting a large text with many tags doesn't take excessive time.
    """
    code = "x = 1\n" * 10
    block = f'<pre><code class="python">{code}</code></pre>\n'
    text = block * 100

    start_time = time.time()
    chunks = split_text_safe(text, max_length=4096)
    end_time = time.time()

    duration = end_time - start_time
    assert duration < 1.0, f"Splitting took too long: {duration:.4f}s"
    assert len(chunks) > 0

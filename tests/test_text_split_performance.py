import time

from app.utils.text_format import split_text_safe


def test_split_text_safe_preserves_attributes_on_split():
    """
    Verifies that when a code block with attributes is split,
    the attributes are preserved in subsequent chunks.
    """
    # Create a code block with specific class
    code = "line\n" * 50
    text = f'<pre><code class="language-python">{code}</code></pre>'

    # Force split by using small max_length
    # The opening tag <pre><code class="language-python"> is ~34 chars.
    # We set max_length to 60 to force split after a few lines.
    chunks = split_text_safe(text, max_length=60)

    assert len(chunks) > 1

    # Check first chunk
    assert chunks[0].startswith('<pre><code class="language-python">')
    assert chunks[0].endswith("</code></pre>")

    # Check second chunk - it MUST start with the same attributes
    # The optimized implementation should restore the full tag string
    assert chunks[1].startswith('<pre><code class="language-python">')


def test_split_text_safe_performance():
    """
    Simple performance smoke test.
    Ensures splitting a large text with many tags doesn't take excessive time.
    """
    # Create a text with many code blocks to trigger the reconstruction logic
    code = "x = 1\n" * 10
    block = f'<pre><code class="python">{code}</code></pre>\n'
    text = block * 100  # ~20k chars

    start_time = time.time()
    chunks = split_text_safe(text, max_length=4096)
    end_time = time.time()

    duration = end_time - start_time
    # It should be very fast (< 0.1s)
    # We set a generous limit to avoid flakes
    assert duration < 1.0, f"Splitting took too long: {duration:.4f}s"
    assert len(chunks) > 0

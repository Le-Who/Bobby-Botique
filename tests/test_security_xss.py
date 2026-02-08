import pytest
from app.utils.text_format import markdown_to_html

def test_xss_javascript_link():
    """Test that javascript: links are stripped."""
    text = "[Click me](javascript:alert(1))"
    output = markdown_to_html(text)
    # The regex logic in markdown_to_html is simplistic and stops at the first closing parenthesis.
    # So [Click me](javascript:alert(1)) matches [Click me](javascript:alert(1) leaving the last )
    # This results in "Click me)"
    # The important part is that the <a> tag is gone.
    assert "javascript" not in output
    assert "<a" not in output
    assert "Click me" in output

def test_xss_vbscript_link():
    """Test that vbscript: links are stripped."""
    text = "[Click me](vbscript:msgbox(1))"
    output = markdown_to_html(text)
    assert "<a" not in output
    assert "vbscript" not in output

def test_xss_data_link():
    """Test that data: links are stripped."""
    text = "[Click me](data:text/html,<script>alert(1)</script>)"
    output = markdown_to_html(text)
    assert "<a" not in output

def test_xss_file_link():
    """Test that file: links are stripped."""
    text = "[Click me](file:///etc/passwd)"
    output = markdown_to_html(text)
    assert "<a" not in output

def test_xss_encoded_javascript():
    """Test that URL-encoded javascript: links are stripped."""
    # javascript:alert(1) -> javascript%3Aalert%281%29
    text = "[Click me](javascript%3Aalert%281%29)"
    output = markdown_to_html(text)
    assert "<a" not in output

def test_xss_case_insensitive():
    """Test mixed case schemes."""
    text = "[Click me](JaVaScRiPt:alert(1))"
    output = markdown_to_html(text)
    assert "<a" not in output

def test_valid_http_link():
    """Test that valid http links work."""
    text = "[Google](http://google.com)"
    output = markdown_to_html(text)
    assert output == '<a href="http://google.com">Google</a>'

def test_valid_https_link():
    """Test that valid https links work."""
    text = "[Google](https://google.com)"
    output = markdown_to_html(text)
    assert output == '<a href="https://google.com">Google</a>'

def test_valid_mailto_link():
    """Test that valid mailto links work."""
    text = "[Email](mailto:user@example.com)"
    output = markdown_to_html(text)
    assert output == '<a href="mailto:user@example.com">Email</a>'

def test_valid_telegram_link():
    """Test that valid telegram deep links work."""
    text = "[User](tg://user?id=123456)"
    output = markdown_to_html(text)
    assert output == '<a href="tg://user?id=123456">User</a>'

def test_link_with_entities():
    """Test that links with entities are handled correctly."""
    # http://example.com?a=1&b=2
    # In markdown input (before html.escape), user might type:
    text = "[Link](http://example.com?a=1&b=2)"
    # markdown_to_html escapes text first -> http://example.com?a=1&amp;b=2
    # Then checks scheme.
    output = markdown_to_html(text)
    # The output href should have &amp;
    assert 'href="http://example.com?a=1&amp;b=2"' in output

def test_link_with_quotes():
    """Test that links with quotes are escaped correctly in href."""
    text = '[Link](http://example.com/foo"bar)'
    output = markdown_to_html(text)
    # " is escaped to &quot;
    assert 'href="http://example.com/foo&quot;bar"' in output

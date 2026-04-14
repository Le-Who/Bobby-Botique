"""Tests for app.utils.json_utils — JSON extraction from noisy model output."""


from app.utils.json_utils import extract_json_object


class TestExtractJsonObject:
    """extract_json_object should parse JSON from noisy LLM output."""

    # ── Valid extractions ────────────────────────────────────────────────

    def test_clean_json(self):
        text = '{"title": "Test", "purpose": "Testing", "prompt": "Hello"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "Test"
        assert result["purpose"] == "Testing"
        assert result["prompt"] == "Hello"

    def test_json_with_code_fence(self):
        text = '```json\n{"title": "Test", "purpose": "Testing", "prompt": "Hello"}\n```'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "Test"

    def test_json_with_surrounding_text(self):
        text = 'Here is the result: {"title": "T", "purpose": "P", "prompt": "Q"} and more text'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_system_prompt_normalized_to_prompt(self):
        text = '{"title": "T", "purpose": "P", "system_prompt": "SP"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["prompt"] == "SP"

    # ── Missing required fields ──────────────────────────────────────────

    def test_missing_title_returns_none(self):
        text = '{"purpose": "P", "prompt": "Q"}'
        assert extract_json_object(text) is None

    def test_missing_purpose_returns_none(self):
        text = '{"title": "T", "prompt": "Q"}'
        assert extract_json_object(text) is None

    def test_missing_prompt_and_system_prompt_returns_none(self):
        text = '{"title": "T", "purpose": "P"}'
        assert extract_json_object(text) is None

    # ── Edge cases ───────────────────────────────────────────────────────

    def test_empty_string_returns_none(self):
        assert extract_json_object("") is None

    def test_none_returns_none(self):
        # The function checks `not text` which is True for None
        assert extract_json_object(None) is None

    def test_no_json_returns_none(self):
        assert extract_json_object("This is just plain text") is None

    def test_invalid_json_returns_none(self):
        assert extract_json_object("{broken json}") is None

    def test_nested_braces_handled(self):
        text = '{"title": "T", "purpose": "P", "prompt": "use {x} and {y}"}'
        result = extract_json_object(text)
        assert result is not None
        assert "{x}" in result["prompt"]

    def test_json_in_array(self):
        text = '[{"title": "T", "purpose": "P", "prompt": "Q"}]'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_multiple_objects_first_invalid(self):
        text = '{"broken": } {"title": "T", "purpose": "P", "prompt": "Q"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_multiple_objects_first_missing_fields(self):
        text = '{"foo": "bar"} {"title": "T", "purpose": "P", "prompt": "Q"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_escaped_quotes_and_backslashes(self):
        text = '{"title": "T", "purpose": "P", "prompt": "Quotes: \\" and Backslash: \\\\"}'
        result = extract_json_object(text)
        assert result is not None
        assert '\\"' in text  # raw text has \"
        assert result["prompt"] == 'Quotes: " and Backslash: \\'

    def test_unicode_content(self):
        text = '{"title": "🔥", "purpose": "🚀", "prompt": "你好"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "🔥"
        assert result["prompt"] == "你好"

    def test_windows_line_endings(self):
        text = '```json\r\n{\r\n"title": "T",\r\n"purpose": "P",\r\n"prompt": "Q"\r\n}\r\n```'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_json_prefix_no_fence(self):
        text = 'json {"title": "T", "purpose": "P", "prompt": "Q"}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

    def test_very_noisy_output(self):
        text = """
        I have thought about it.
        The result is:
        {
           "irrelevant": true
        }
        Wait, I mean:
        ```json
        {
            "title": "Final",
            "purpose": "Test",
            "prompt": "Go"
        }
        ```
        Hope this helps!
        """
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "Final"

    def test_nested_objects_with_required_fields_inner(self):
        # Outer object is valid JSON but missing fields.
        # Inner object has required fields.
        text = '{"outer": {"title": "T", "purpose": "P", "prompt": "Q"}}'
        result = extract_json_object(text)
        assert result is not None
        assert result["title"] == "T"

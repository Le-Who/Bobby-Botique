"""Tests for app.prompt_registry — versioned prompt templates and composition."""

import threading

import pytest

from app.prompt_registry import (
    FORMATTING_RULES,
    FORMATTING_RULES_COMPACT,
    IMAGE_ANALYSIS,
    PROMPT_ENGINEER,
    QNA_LOCALIZATION,
    SYNTHESIS,
    SYSTEM_PROMPT_COMPACT,
    SYSTEM_PROMPT_FULL,
    URL_SELECTION,
    PromptRegistry,
    PromptTemplate,
    estimate_tokens_cyrillic,
    get_registry,
    reset_registry,
)

# ── Token estimation ─────────────────────────────────────────────────────────


class TestEstimateTokensCyrillic:
    def test_empty_string(self):
        assert estimate_tokens_cyrillic("") == 0

    def test_ascii_text(self):
        tokens = estimate_tokens_cyrillic("Hello world")
        assert tokens > 0
        # ASCII: ~11 bytes UTF-8 // 3 ≈ 3
        assert tokens == len(b"Hello world") // 3

    def test_cyrillic_text(self):
        text = "Привет мир"
        tokens = estimate_tokens_cyrillic(text)
        # Cyrillic: 2 bytes per char in UTF-8, so ~20 bytes // 3 ≈ 6
        # Old method: len("Привет мир") // 4 = 10 // 4 = 2 (underestimates!)
        old_estimate = len(text) // 4
        assert tokens > old_estimate, "Cyrillic estimation should be higher than naive len//4"

    def test_mixed_text(self):
        text = "Hello Привет 123"
        tokens = estimate_tokens_cyrillic(text)
        assert tokens > 0

    def test_minimum_one_token(self):
        assert estimate_tokens_cyrillic("a") >= 1


# ── PromptTemplate ────────────────────────────────────────────────────────────


class TestPromptTemplate:
    def test_auto_token_estimation(self):
        tmpl = PromptTemplate(name="test", version="1.0", text="Hello world", purpose="test")
        assert tmpl.estimated_tokens > 0

    def test_explicit_token_count(self):
        tmpl = PromptTemplate(
            name="test",
            version="1.0",
            text="Hello world",
            purpose="test",
            estimated_tokens=42,
        )
        assert tmpl.estimated_tokens == 42

    def test_immutable(self):
        tmpl = PromptTemplate(name="test", version="1.0", text="Hello", purpose="test")
        with pytest.raises(AttributeError):
            tmpl.name = "changed"

    def test_all_defaults_have_versions(self):
        for tmpl in (
            SYSTEM_PROMPT_FULL,
            SYSTEM_PROMPT_COMPACT,
            QNA_LOCALIZATION,
            URL_SELECTION,
            SYNTHESIS,
            IMAGE_ANALYSIS,
            PROMPT_ENGINEER,
        ):
            assert tmpl.version, f"{tmpl.name} has no version"
            assert tmpl.name, "Template has no name"
            assert tmpl.purpose, f"{tmpl.name} has no purpose"


# ── PromptRegistry ────────────────────────────────────────────────────────────


class TestPromptRegistry:
    def setup_method(self):
        reset_registry()

    def test_get_default_templates(self):
        registry = PromptRegistry()
        assert registry.get("system_prompt_full") is not None
        assert registry.get("system_prompt_compact") is not None
        assert registry.get("qna_localization") is not None
        assert registry.get("url_selection") is not None
        assert registry.get("synthesis") is not None
        assert registry.get("image_analysis") is not None
        assert registry.get("prompt_engineer") is not None

    def test_get_nonexistent(self):
        registry = PromptRegistry()
        assert registry.get("nonexistent") is None

    def test_register_custom(self):
        registry = PromptRegistry()
        custom = PromptTemplate(
            name="custom_test",
            version="1.0",
            text="Custom prompt",
            purpose="Testing",
        )
        registry.register(custom)
        assert registry.get("custom_test") is not None
        assert registry.get("custom_test").text == "Custom prompt"

    def test_register_overwrite(self):
        registry = PromptRegistry()
        custom_v1 = PromptTemplate(name="custom", version="1.0", text="v1", purpose="test")
        custom_v2 = PromptTemplate(name="custom", version="2.0", text="v2", purpose="test")
        registry.register(custom_v1)
        registry.register(custom_v2)
        assert registry.get("custom").version == "2.0"

    def test_list_templates(self):
        registry = PromptRegistry()
        templates = registry.list_templates()
        assert len(templates) == 9  # 7 original + 2 summarization templates

    def test_version_info(self):
        registry = PromptRegistry()
        versions = registry.get_version_info()
        assert "system_prompt_full" in versions
        assert versions["system_prompt_full"] == SYSTEM_PROMPT_FULL.version


# ── compose_system_prompt ─────────────────────────────────────────────────────


class TestComposeSystemPrompt:
    def setup_method(self):
        reset_registry()

    def test_no_role_returns_full_prompt(self):
        registry = PromptRegistry()
        result = registry.compose_system_prompt(role_prompt=None)
        assert "РОЛЬ И ЗАДАЧА" in result
        assert "ИИ-ассистент" in result
        # Should contain formatting rules (embedded)
        assert "РАЗРЕШЕНО" in result

    def test_with_role_uses_compact_by_default(self):
        registry = PromptRegistry()
        result = registry.compose_system_prompt(role_prompt="Ты — преподаватель математики.")
        # Should have compact system prompt
        assert "ДОПОЛНИТЕЛЬНАЯ РОЛЬ" in result
        assert "Ты — преподаватель математики." in result
        # Should NOT have full system prompt sections
        assert "FEW-SHOT ПРИМЕРЫ" not in result

    def test_with_role_full_mode(self):
        registry = PromptRegistry()
        result = registry.compose_system_prompt(role_prompt="Ты — учитель.", use_compact=False)
        assert "ДОПОЛНИТЕЛЬНАЯ РОЛЬ" in result
        assert "Ты — учитель." in result
        # Should have full system prompt sections
        assert "РОЛЬ И ЗАДАЧА" in result

    def test_caching_works(self):
        registry = PromptRegistry()
        result1 = registry.compose_system_prompt(role_prompt="Test role")
        result2 = registry.compose_system_prompt(role_prompt="Test role")
        assert result1 is result2  # Same object from cache

    def test_different_roles_different_results(self):
        registry = PromptRegistry()
        result1 = registry.compose_system_prompt(role_prompt="Role A")
        result2 = registry.compose_system_prompt(role_prompt="Role B")
        assert result1 != result2


# ── get_task_prompt ───────────────────────────────────────────────────────────


class TestGetTaskPrompt:
    def test_qna_localization(self):
        registry = PromptRegistry()
        result = registry.get_task_prompt(
            "qna_localization",
            user_message="Что такое Python?",
            tavily_answer="Python is a programming language",
        )
        assert "Что такое Python?" in result
        assert "Python is a programming language" in result
        # Should contain formatting rules
        assert "РАЗРЕШЕНО" in result

    def test_url_selection(self):
        registry = PromptRegistry()
        result = registry.get_task_prompt(
            "url_selection",
            user_message="Docker setup",
            search_results_json="[{url: 'https://docs.docker.com'}]",
        )
        assert "Docker setup" in result

    def test_unknown_template_raises(self):
        registry = PromptRegistry()
        with pytest.raises(KeyError, match="not found"):
            registry.get_task_prompt("nonexistent")


# ── Shared formatting rules ──────────────────────────────────────────────────


class TestFormattingRules:
    def test_formatting_rules_content(self):
        assert "РАЗРЕШЕНО" in FORMATTING_RULES
        assert "ЗАПРЕЩЕНО" in FORMATTING_RULES
        assert "МАТЕМАТИКА" in FORMATTING_RULES

    def test_compact_rules_shorter(self):
        assert len(FORMATTING_RULES_COMPACT) < len(FORMATTING_RULES)

    def test_no_duplicate_rules_in_system_prompts(self):
        """Formatting rules should be referenced via placeholder, not hardcoded."""
        # The templates should use {formatting_rules} placeholder
        assert "{formatting_rules}" in SYSTEM_PROMPT_FULL.text
        assert "{formatting_rules_compact}" in SYSTEM_PROMPT_COMPACT.text


# ── Thread safety ─────────────────────────────────────────────────────────────


class TestThreadSafety:
    def test_singleton_thread_safe(self):
        reset_registry()
        results = []
        errors = []

        def get_reg():
            try:
                reg = get_registry()
                results.append(id(reg))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_reg) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All threads should get the same instance
        assert len(set(results)) == 1


# ── No jailbreak markers ─────────────────────────────────────────────────────


class TestNoJailbreakMarkers:
    """Ensure prompt templates don't contain words that trigger Gemini internal guardrails.

    Gemini 2.5/3.x models detect 'ignore safety'-style phrases as jailbreak
    attempts and respond with *more* refusals.  This test prevents regressions.
    """

    JAILBREAK_MARKERS = [
        "игнорируя безопасность",
        "игнорируй безопасность",
        "игнорируя возможные проблемы с безопасностью",
        "ignore safety",
        "bypass safety",
        "обойти безопасность",
        "обойти ограничения",
        "без ограничений",
        "проблемы с безопасностью",
        "ответственность на себя берёт",
    ]

    def test_no_jailbreak_markers_in_registry_templates(self):
        registry = PromptRegistry()
        for tmpl in registry.list_templates():
            text_lower = tmpl.text.lower()
            for marker in self.JAILBREAK_MARKERS:
                assert marker.lower() not in text_lower, f"Template '{tmpl.name}' contains jailbreak marker '{marker}'"

    def test_no_jailbreak_markers_in_legacy_prompt(self):
        from app.prompts import PROMPT_ENGINEER_SYSTEM_PROMPT

        text_lower = PROMPT_ENGINEER_SYSTEM_PROMPT.lower()
        for marker in self.JAILBREAK_MARKERS:
            assert marker.lower() not in text_lower, (
                f"PROMPT_ENGINEER_SYSTEM_PROMPT contains jailbreak marker '{marker}'"
            )

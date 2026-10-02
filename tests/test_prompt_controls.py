"""Prompt edits affect live composition and preserve validated, coherent snapshots."""

import pytest

from app import prompt_registry as registry_module
from app.runtime_settings import store


@pytest.fixture
def prompt_runtime(monkeypatch):
    """Use immutable settings snapshots without a database in prompt-layer tests."""
    from types import MappingProxyType

    from app.runtime_settings import prompts

    registry_module.reset_registry()
    state = {"snapshot": store.SettingsSnapshot(0, MappingProxyType({}))}

    async def get_snapshot(*, force=False):
        return state["snapshot"]

    async def set_value(key, value, *, expected_revision, actor):
        current = state["snapshot"]
        assert expected_revision == current.revision
        values = dict(current.values)
        values[key] = value
        state["snapshot"] = store.SettingsSnapshot(current.revision + 1, MappingProxyType(values))
        return state["snapshot"]

    async def reset_value(key, *, expected_revision, actor):
        current = state["snapshot"]
        assert expected_revision == current.revision
        values = dict(current.values)
        values.pop(key, None)
        state["snapshot"] = store.SettingsSnapshot(current.revision + 1, MappingProxyType(values))
        return state["snapshot"]

    monkeypatch.setattr(prompts.store, "get_snapshot", get_snapshot)
    monkeypatch.setattr(prompts.store, "set_value", set_value)
    monkeypatch.setattr(prompts.store, "reset_value", reset_value)
    yield prompts, state
    registry_module.get_registry().apply_overrides({}, revision=1_000_000)
    registry_module.reset_registry()


def test_prompt_catalog_and_atomic_cache_refresh():
    registry = registry_module.PromptRegistry()
    original = registry.compose_system_prompt()
    registry.apply_overrides(
        {"system_prompt_full": "Changed {formatting_rules}", "formatting_rules": "Formatting changed"},
        revision=1,
    )
    assert registry.compose_system_prompt().startswith("Changed Formatting changed")
    assert registry.compose_system_prompt() != original
    registry.apply_overrides({}, revision=2)
    assert registry.compose_system_prompt() == original


def test_invalid_snapshot_never_partially_changes_live_prompts():
    registry = registry_module.PromptRegistry()
    original = registry.compose_system_prompt()
    with pytest.raises(ValueError):
        registry.apply_overrides(
            {"formatting_rules": "changed", "qna_localization": "Lost required variables"}, revision=1
        )
    assert registry.compose_system_prompt() == original


@pytest.mark.parametrize(
    "text",
    ["", " ", "Hello {unknown}", "Hello {user.name}", "x" * 65537],
    ids=["empty", "whitespace", "unknown", "attribute", "too_long"],
)
def test_rejects_invalid_prompt_text(text):
    registry = registry_module.PromptRegistry()
    with pytest.raises(ValueError):
        registry.apply_overrides({"prompt_engineer": text}, revision=1)


def test_json_braces_allowed_and_roles_preserve_imported_dict_identity():
    registry = registry_module.PromptRegistry()
    roles = registry_module.DEFAULT_ROLES
    old_role = roles["teacher"]["prompt"]
    try:
        registry.apply_overrides(
            {"role.teacher": "New teacher", "prompt_engineer": 'Return {"title": "name"}'}, revision=1
        )
        assert roles is registry_module.DEFAULT_ROLES
        assert roles["teacher"]["prompt"] == "New teacher"
        assert old_role != "New teacher"  # Previously copied conversation prompt remains independent.
        assert registry.get_task_prompt("prompt_engineer") == 'Return {"title": "name"}'
    finally:
        registry.apply_overrides({}, revision=2)


def test_unknown_prompt_rejected():
    registry = registry_module.PromptRegistry()
    with pytest.raises(KeyError):
        registry.apply_overrides({"missing": "text"}, revision=1)


@pytest.mark.parametrize(
    "name",
    [
        "qna_localization",
        "url_selection",
        "synthesis",
        "summarization_system",
        "summarization_chunk",
        "summarization_refine_first",
        "summarization_refine_subsequent",
    ],
)
async def test_legacy_prompt_cards_do_not_accept_ineffective_live_edits(prompt_runtime, name):
    prompts, state = prompt_runtime
    row = next(row for row in registry_module.get_registry().prompt_catalog() if row["id"] == name)
    assert row["editable"] is False
    assert row["apply"] == "inactive"
    assert row["note"]
    with pytest.raises(ValueError, match="не используется"):
        await prompts.save_prompt(name, row["baseline"], expected_revision=0, actor="admin")
    with pytest.raises(ValueError, match="не используется"):
        await prompts.reset_prompt(name, expected_revision=0, actor="admin")
    assert state["snapshot"].revision == 0


def test_historical_legacy_override_remains_loadable_without_claiming_runtime_effect():
    from types import MappingProxyType

    from app.runtime_settings import prompts

    baseline = registry_module._PROMPT_BASELINES["summarization_system"]
    snapshot = store.SettingsSnapshot(101, MappingProxyType({"prompt:summarization_system": baseline + " Historical"}))
    assert prompts._overrides(snapshot.values) == {"summarization_system": baseline + " Historical"}


@pytest.mark.asyncio
async def test_save_and_reset_prompt_update_live_composition(prompt_runtime):
    prompts, state = prompt_runtime
    baseline = registry_module.get_registry().compose_system_prompt()
    changed = "Live edit {formatting_rules}"

    await prompts.save_prompt("system_prompt_full", changed, expected_revision=0, actor="admin")
    assert registry_module.get_prompt_text("system_prompt_full") == changed
    assert registry_module.get_registry().compose_system_prompt().startswith("Live edit # ПРАВИЛА")
    assert state["snapshot"].values["prompt:system_prompt_full"] == changed
    rows = await prompts.list_prompts()
    assert next(row for row in rows if row["id"] == "system_prompt_full")["source"] == "override"

    await prompts.reset_prompt("system_prompt_full", expected_revision=1, actor="admin")
    assert registry_module.get_registry().compose_system_prompt() == baseline
    assert "prompt:system_prompt_full" not in state["snapshot"].values


@pytest.mark.asyncio
async def test_refresh_rejects_invalid_snapshot_without_partial_application(prompt_runtime):
    prompts, state = prompt_runtime
    registry = registry_module.get_registry()
    baseline = registry.compose_system_prompt()
    from types import MappingProxyType

    state["snapshot"] = store.SettingsSnapshot(
        4,
        MappingProxyType(
            {
                "prompt:formatting_rules": "Changed rules",
                "prompt:qna_localization": "Missing required fields",
            }
        ),
    )
    with pytest.raises(ValueError):
        await prompts.refresh_prompts(force=True)
    assert registry.compose_system_prompt() == baseline
    assert registry.revision == -1


@pytest.mark.asyncio
async def test_save_validates_before_persist_and_rejects_unknown_id(prompt_runtime):
    prompts, state = prompt_runtime
    for name, value in (
        ("missing", "text"),
        ("system_prompt_full", "Lost formatting rules"),
        ("prompt_engineer", "Hello {unknown}"),
        ("prompt_engineer", "Hello {user.name}"),
    ):
        with pytest.raises((KeyError, ValueError)):
            await prompts.save_prompt(name, value, expected_revision=0, actor="admin")
    assert state["snapshot"].revision == 0


@pytest.mark.asyncio
async def test_same_revision_refresh_preserves_register_until_revision_changes(prompt_runtime):
    prompts, state = prompt_runtime
    from types import MappingProxyType

    registry = registry_module.get_registry()
    await prompts.refresh_prompts()
    custom = registry_module.PromptTemplate(
        name="system_prompt_full",
        version="test",
        text="local {formatting_rules}",
        purpose="local",
    )
    registry.register(custom)
    await prompts.refresh_prompts()
    assert registry.get("system_prompt_full") is custom
    state["snapshot"] = store.SettingsSnapshot(1, MappingProxyType({}))
    await prompts.refresh_prompts(force=True)
    assert registry.get("system_prompt_full") is not custom


def test_controlled_prompt_registration_exposes_live_text_and_preserves_variables(monkeypatch):
    from app.prompt_registry import get_registry, register_controlled_text

    monkeypatch.setattr(registry_module, "_PROMPT_BASELINES", dict(registry_module._PROMPT_BASELINES))
    monkeypatch.setattr(registry_module, "_PROMPT_TITLES", dict(registry_module._PROMPT_TITLES))
    monkeypatch.setattr(registry_module, "_registry_instance", registry_module.PromptRegistry())
    register_controlled_text("test.controlled", "Answer {topic}", "Test controlled")
    registry = get_registry()
    assert registry.get_prompt_text("test.controlled") == "Answer {topic}"
    registry.apply_overrides({"test.controlled": "Explain {topic}"}, revision=registry.revision + 1)
    assert registry.get_prompt_text("test.controlled") == "Explain {topic}"
    assert any(row["id"] == "test.controlled" for row in registry.prompt_catalog())


def test_prompt_scope_keeps_inflight_text_when_admin_updates(monkeypatch):
    registry = registry_module.PromptRegistry()
    monkeypatch.setattr(registry_module, "_registry_instance", registry)
    original = registry.get_prompt_text("formatting_rules")
    with registry_module.prompt_scope():
        registry.apply_overrides({"formatting_rules": "New formatting"}, revision=1)
        assert registry_module.get_prompt_text("formatting_rules") == original
        assert "New formatting" not in registry_module.get_registry().compose_system_prompt()
    assert registry_module.get_prompt_text("formatting_rules") == "New formatting"


def test_prompt_scope_can_pin_older_revision_without_reverting_global(monkeypatch):
    registry = registry_module.PromptRegistry()
    monkeypatch.setattr(registry_module, "_registry_instance", registry)
    registry.apply_overrides({"formatting_rules": "Latest"}, revision=12)
    with registry_module.prompt_scope(overrides={"formatting_rules": "Earlier"}, revision=11):
        assert registry_module.get_prompt_text("formatting_rules") == "Earlier"
        assert registry_module.get_registry().revision == 11
        assert "Earlier" in registry_module.get_registry().compose_system_prompt()
        assert registry_module.FORMATTING_RULES == "Latest"
    assert registry.get_prompt_text("formatting_rules") == "Latest"
    registry.apply_overrides({}, revision=13)


def test_template_rendering_preserves_literal_json_and_does_not_reparse_user_values():
    from app.prompt_registry import render_prompt_text

    text = 'Example: {"hints":["one"]}; escaped {{"word":"two"}}; user {caption}; rules {rules}'
    rendered = render_prompt_text(text, caption="literal {rules}", rules="fixed rules")
    assert rendered == 'Example: {"hints":["one"]}; escaped {"word":"two"}; user literal {rules}; rules fixed rules'
    with pytest.raises(ValueError):
        render_prompt_text("Required {caption}")


def test_template_rendering_preserves_nested_json_and_escaped_examples():
    from app.prompt_registry import render_prompt_text

    text = 'Raw {"x":{"y":1}}; legacy {{"x":{{"y":2}}}}; mixed {{"x":{"y":3}}}; {caption}'
    assert render_prompt_text(text, caption="literal {caption}") == (
        'Raw {"x":{"y":1}}; legacy {"x":{"y":2}}; mixed {"x":{"y":3}}; literal {caption}'
    )


def test_task_prompt_does_not_substitute_fields_inside_inserted_user_text():
    registry = registry_module.PromptRegistry()
    result = registry.get_task_prompt(
        "qna_localization", user_message="literal {tavily_answer}", tavily_answer="Answer"
    )
    assert "literal {tavily_answer}" in result


def test_text_is_a_valid_template_variable():
    assert registry_module.render_prompt_text("Input: {text}", text="user input") == "Input: user input"


def test_template_braces_inside_json_strings_are_data():
    from app.prompt_registry import render_prompt_text

    text = 'Example {{"brace":"}","nested":{"x":"{\\""}}}; {caption}'
    assert render_prompt_text(text, caption="value") == 'Example {"brace":"}","nested":{"x":"{\\""}}; value'

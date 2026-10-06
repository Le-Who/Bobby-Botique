"""Inventory follows source declarations without executing application modules."""

from types import SimpleNamespace

import pytest


def test_source_scan_discovers_new_prompts_and_consumers_without_importing(tmp_path):
    from app.runtime_settings.source_inventory import scan_sources

    module = tmp_path / "app" / "feature.py"
    module.parent.mkdir()
    module.write_text(
        """raise AssertionError("inventory must not execute source")
register_controlled_text("feature.system", "Answer {query}", "Feature")
async def answer():
    prompt = get_prompt_text("feature.system")
    return await execute_text_process(process_id="feature.answer", baseline=("model",), history=[])
""",
        encoding="utf-8",
    )
    inventory = scan_sources(tmp_path)
    assert inventory.prompt_modules == ("app.feature",)
    assert inventory.prompt_definitions["feature.system"][0].file == "app/feature.py"
    assert inventory.prompt_consumers["feature.system"][0].function == "answer"
    assert inventory.process_consumers["feature.answer"][0].line == 5

    module.unlink()
    removed = scan_sources(tmp_path)
    assert removed.prompt_modules == ()
    assert not removed.prompt_definitions
    assert not removed.process_consumers


def test_source_scan_follows_conditional_ids_and_reports_dynamic_readers(tmp_path):
    from app.runtime_settings.source_inventory import scan_sources

    module = tmp_path / "app" / "feature.py"
    module.parent.mkdir()
    module.write_text(
        """async def answer(lane, name):
    get_prompt_text("feature.main" if lane else "feature.reserve")
    get_prompt_text(name)
    await generation_request_from_history(process_id="feature.answer", models=[], history=[])
    await generate_daily_text_for("judge", prompt="question")
""",
        encoding="utf-8",
    )
    inventory = scan_sources(tmp_path)
    assert set(inventory.prompt_consumers) == {"feature.main", "feature.reserve"}
    assert set(inventory.process_consumers) == {"feature.answer", "crocodile.judge"}
    assert any(row.kind == "prompt" and row.expression == "name" for row in inventory.dynamic_references)


def test_prompt_loader_imports_discovered_definition_modules(tmp_path, monkeypatch):
    from app import prompt_registry
    from app.runtime_settings import lifecycle

    module = tmp_path / "inventory_probe.py"
    module.write_text(
        "from app.prompt_registry import register_controlled_text\n"
        'register_controlled_text("probe.system", "Answer {query}", "Probe")\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(prompt_registry, "_PROMPT_BASELINES", dict(prompt_registry._PROMPT_BASELINES))
    monkeypatch.setattr(prompt_registry, "_PROMPT_TITLES", dict(prompt_registry._PROMPT_TITLES))
    monkeypatch.setattr(
        lifecycle, "get_source_inventory", lambda: SimpleNamespace(prompt_modules=("inventory_probe",)), raising=False
    )
    prompt_registry.reset_registry()
    try:
        lifecycle.load_controlled_prompts()
        assert prompt_registry.get_prompt_text("probe.system") == "Answer {query}"
    finally:
        prompt_registry.reset_registry()


def test_removed_prompt_override_does_not_prevent_loading_active_prompts():
    from app.runtime_settings.prompts import _overrides

    active = _overrides({"prompt:removed.feature": "old text", "prompt:formatting_rules": "current rules"})
    assert active == {"formatting_rules": "current rules"}


def test_checkout_process_calls_are_registered_and_prompt_modules_are_loaded():
    from app.process_policies import PROCESSES
    from app.prompt_registry import get_registry
    from app.runtime_settings.lifecycle import load_controlled_prompts
    from app.runtime_settings.source_inventory import get_source_inventory

    inventory = get_source_inventory()
    assert set(inventory.process_consumers) <= set(PROCESSES), "Register new model routes before shipping"
    connected = {identity for identity, spec in PROCESSES.items() if spec.connected}
    assert connected <= set(inventory.process_consumers), "Remove retired routes or describe their dynamic readers"
    load_controlled_prompts()
    ids = {row["id"] for row in get_registry().prompt_catalog()}
    assert set(inventory.prompt_definitions) <= ids, "New prompt definitions must reach the admin catalog"
    assert set(inventory.prompt_consumers) <= ids, "Remove stale prompt readers with their definitions"


@pytest.mark.asyncio
async def test_dashboard_process_evidence_follows_consumers_and_exposes_unregistered_routes(tmp_path, monkeypatch):
    from app import process_policies
    from app.runtime_settings import lifecycle, source_inventory
    from app.runtime_settings.store import SettingsSnapshot

    module = tmp_path / "app" / "probe.py"
    module.parent.mkdir()
    module.write_text(
        'async def run():\n    await resolve_process("chat", ())\n    await resolve_process("future.route", ())\n',
        encoding="utf-8",
    )
    inventory = source_inventory.scan_sources(tmp_path)
    monkeypatch.setattr(source_inventory, "get_source_inventory", lambda: inventory)

    async def snapshot():
        return SettingsSnapshot(0, {})

    async def baseline(process_id):
        return ("baseline",)

    monkeypatch.setattr(lifecycle, "operation_snapshot", snapshot)
    monkeypatch.setattr(process_policies, "baseline_models", baseline)
    rows = {row["id"]: row for row in await process_policies.list_processes()}
    assert rows["chat"]["evidence"] == [{"file": "app/probe.py", "function": "run", "line": 2}]
    assert rows["future.route"]["editable"] is False
    assert rows["future.route"]["evidence"][0]["line"] == 3


def test_prompt_metadata_includes_discovered_definitions_and_readers(tmp_path, monkeypatch):
    from app.runtime_settings import prompt_usage, source_inventory

    module = tmp_path / "app" / "probe.py"
    module.parent.mkdir()
    module.write_text(
        'register_controlled_text("probe.system", "Answer", "Probe")\n'
        'def answer():\n    return get_prompt_text("probe.system")\n',
        encoding="utf-8",
    )
    inventory = source_inventory.scan_sources(tmp_path)
    monkeypatch.setattr(source_inventory, "get_source_inventory", lambda: inventory)
    metadata = prompt_usage.prompt_control_metadata("probe.system")
    assert metadata["evidence"] == [{"file": "app/probe.py", "function": "answer", "line": 3}]
    assert metadata["definitions"] == [{"file": "app/probe.py", "function": "<module>", "line": 1}]

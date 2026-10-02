"""Keep the admin execution map backed by concrete readers in this checkout."""

import ast
from pathlib import Path

import pytest

from app.process_policies import PROCESSES

ROOT = Path(__file__).resolve().parents[1]


def _function(path: str, qualified_name: str):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    current = tree
    for name in qualified_name.split("."):
        current = next(
            node
            for node in current.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
        )
    return current


def test_every_process_has_reviewable_execution_evidence():
    from app.runtime_settings.process_evidence import PROCESS_EVIDENCE

    assert set(PROCESS_EVIDENCE) == set(PROCESSES)
    for process, row in PROCESS_EVIDENCE.items():
        assert row["executor_family"] and row["scope"] and row["cache_note"], process
        assert 1 <= len(row["evidence"]) <= 2, process
        for location in row["evidence"]:
            node = _function(location["file"], location["function"])
            assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)), process


@pytest.mark.parametrize("process", sorted(PROCESSES))
def test_connected_process_evidence_points_to_actual_policy_consumption(process):
    from app.runtime_settings.process_evidence import PROCESS_EVIDENCE

    row = PROCESS_EVIDENCE[process]
    nodes = [_function(location["file"], location["function"]) for location in row["evidence"]]
    if not PROCESSES[process].connected:
        assert process == "embedding"
        assert row["apply"] == "manual_migration"
        assert any(
            isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "embed_content"
            for function in nodes
            for node in ast.walk(function)
        )
        return
    routing_functions = {
        "resolve_process",
        "execute_text_process",
        "run_gemini_override",
        "run_result_process",
        "generation_request_from_history",
        "_get_ai_response_with_routing",
        "_generate_controlled_media",
    }
    role = process.removeprefix("crocodile.")
    for function in nodes:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            values = {
                argument.value
                for argument in arguments
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            }
            if name in routing_functions and process in values:
                return
            if (
                process.startswith("crocodile.")
                and name in {"get_daily_text_model_for", "generate_daily_text_for"}
                and role in values
            ):
                return
    pytest.fail(f"{process}: evidence no longer points to a policy consumer")


def test_live_timing_and_uneditable_embeddings_are_explicit():
    from app.runtime_settings.process_evidence import process_evidence

    assert process_evidence("live")["apply"] == "new_session"
    assert process_evidence("live.vertex")["apply"] == "new_session"
    assert process_evidence("embedding")["apply"] == "manual_migration"

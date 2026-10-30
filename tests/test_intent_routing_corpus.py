"""Offline routing evidence must fail on unsafe decisions, independently of fixture labels."""

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "intent_routing_cases.json"


def evaluator():
    try:
        return importlib.import_module("scripts.evaluate_intent_routing")
    except ModuleNotFoundError:
        pytest.fail("The offline routing evaluator has not been implemented")


def case(**overrides):
    value = {
        "case_id": "synthetic-regression",
        "group": "regression",
        "language": "ru",
        "input": "Привет",
        "history": [],
        "explicit_thinking": None,
        "expected_route": "llm",
        "allowed_fallback": [],
        "expected_thinking": "low",
        "forbidden_side_effect": ["network", "tool_execution", "direct_delivery", "tts_delivery"],
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("name", [".env", ".env.production", "prompts.json"])
def test_corpus_loader_refuses_sensitive_paths_before_reading(tmp_path, name):
    path = tmp_path / name
    path.write_text("synthetic content that is deliberately not JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="Sensitive"):
        evaluator().load_cases(path)


@pytest.mark.asyncio
async def test_offline_evaluator_runs_real_currency_parser_with_literal_direction():
    result = await evaluator().evaluate_cases(
        [
            case(
                input="convert 120 EUR to USD",
                expected_route="currency",
                forbidden_side_effect=["network", "tool_execution"],
            )
        ]
    )
    assert result["failed_case_ids"] == []
    assert result["cases"][0]["route"] == "currency"
    assert result["cases"][0]["fixture_requests"] == [{"kind": "currency", "base": "EUR", "target": "USD"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "thinking"),
    [
        ("Погода в Лондоне, напиши стих", "medium"),
        ("Курс EUR USD, переведи слово дом", "low"),
        ("Гороскоп овен, напиши стих", "medium"),
    ],
)
async def test_comma_separated_authored_task_stays_in_chat(text, thinking):
    report = await evaluator().evaluate_cases([case(input=text, expected_thinking=thinking)])
    assert report["failed_case_ids"] == []
    assert report["cases"][0]["route"] == "llm"
    assert report["cases"][0]["reason_code"] == "multiple_tasks"
    assert report["cases"][0]["fixture_requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Переведи 'погода в Лондоне'",
        "Переведи ‘погода в Лондоне’",
        "Переведи 'курс USD EUR'",
        "Переведи ‘курс USD EUR’",
        "Переведи 'гороскоп овен'",
        "Переведи ‘гороскоп овен’",
    ],
)
async def test_balanced_single_quotes_keep_direct_commands_as_data(text):
    report = await evaluator().evaluate_cases([case(input=text)])
    assert report["failed_case_ids"] == []
    assert report["cases"][0]["reason_code"] == "non_direct_request"
    assert report["cases"][0]["fixture_requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Он сказал 'озвучь ответ'", "Он сказал ‘озвучь ответ’"])
async def test_balanced_single_quotes_do_not_authorize_tts(text):
    report = await evaluator().evaluate_cases([case(input=text, expected_tts=False)])
    assert report["failed_case_ids"] == []
    assert report["cases"][0]["voice_reason_code"] == "non_authored_command"
    assert report["cases"][0]["fixture_requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("apostrophe", ["'", "’", "ʼ"])
async def test_ukrainian_word_apostrophes_do_not_hide_authored_commands(apostrophe):
    cases = [
        case(
            case_id="apostrophe-currency",
            input=f"Курс EUR USD для сім{apostrophe}ї з п{apostrophe}яти людей",
            expected_route="currency",
            forbidden_side_effect=["network", "tool_execution", "tts_delivery"],
        ),
        case(
            case_id="apostrophe-voice",
            input=f"Ответь голосом про п{apostrophe}ять м{apostrophe}ячів",
            expected_tts=True,
            forbidden_side_effect=["network", "tool_execution", "direct_delivery"],
        ),
    ]
    report = await evaluator().evaluate_cases(cases)
    assert report["failed_case_ids"] == []
    assert report["cases"][0]["route"] == "currency"
    assert report["cases"][1]["voice_reason_code"] == "explicit_user_command"


@pytest.mark.asyncio
async def test_safe_missed_shortcut_is_separate_from_unsafe_direct_delivery(monkeypatch):
    from app.intent_router import DirectIntentDecision, IntentResult

    async def unsafe(_text):
        return DirectIntentDecision("weather", "direct_resolved", IntentResult("synthetic card"))

    monkeypatch.setattr("app.intent_router.resolve_direct_intent", unsafe)
    unsafe_report = await evaluator().evaluate_cases([case()])
    assert unsafe_report["false_shortcuts"] == 1
    assert unsafe_report["forbidden_side_effects"] == 1
    assert unsafe_report["failed_case_ids"] == ["synthetic-regression"]

    async def unresolved(_text):
        return DirectIntentDecision("llm", "direct_unresolved")

    monkeypatch.setattr("app.intent_router.resolve_direct_intent", unresolved)
    safe_report = await evaluator().evaluate_cases(
        [case(expected_route="weather", allowed_fallback=["llm"], forbidden_side_effect=["network", "tool_execution"])]
    )
    assert safe_report["missed_shortcuts"] == 1
    assert safe_report["false_shortcuts"] == 0
    assert safe_report["failed_case_ids"] == []


@pytest.mark.asyncio
async def test_evaluator_detects_explicit_off_violation(monkeypatch):
    from app.thinking_classifier import ThinkingDecision

    monkeypatch.setattr(
        "app.thinking_classifier.resolve_thinking_decision",
        lambda *args, **kwargs: ThinkingDecision("high", "high_code"),
    )
    report = await evaluator().evaluate_cases([case(explicit_thinking="off", expected_thinking="off")])
    assert report["explicit_setting_violations"] == 1
    assert report["failed_case_ids"] == ["synthetic-regression"]


@pytest.mark.asyncio
async def test_network_and_tool_attempts_fail_even_if_router_swallows_errors(monkeypatch):
    from app.intent_router import DirectIntentDecision

    async def broken(_text):
        for action in (
            lambda: socket.getaddrinfo("example.invalid", 443),
            lambda: subprocess.Popen(["synthetic-tool"]),
        ):
            try:
                action()
            except RuntimeError:
                pass
        return DirectIntentDecision("llm", "no_direct_match")

    monkeypatch.setattr("app.intent_router.resolve_direct_intent", broken)
    report = await evaluator().evaluate_cases([case()])
    assert report["isolation_violations"] == 2
    assert report["failed_case_ids"] == ["synthetic-regression"]


@pytest.mark.asyncio
async def test_async_connection_boundary_is_blocked_before_os_transport(monkeypatch):
    from app.intent_router import DirectIntentDecision

    async def transport_must_not_run(*args, **kwargs):
        raise AssertionError("The OS transport must remain unreachable")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_connection", transport_must_not_run)

    async def broken(_text):
        try:
            await loop.create_connection(lambda: asyncio.Protocol(), "synthetic.invalid", 443)
        except RuntimeError:
            pass
        return DirectIntentDecision("llm", "no_direct_match")

    monkeypatch.setattr("app.intent_router.resolve_direct_intent", broken)
    report = await evaluator().evaluate_cases([case()])
    assert report["isolation_violations"] == 1
    assert report["failed_case_ids"] == ["synthetic-regression"]


@pytest.mark.asyncio
async def test_fixture_provider_failure_keeps_fallback_distinct_from_success():
    report = await evaluator().evaluate_cases(
        [
            case(
                input="погода в Лондоне",
                fixture_scenario="unavailable",
                expected_route="weather",
                allowed_fallback=["llm"],
                forbidden_side_effect=["network", "tool_execution"],
            )
        ]
    )
    assert report["cases"][0]["route"] == "llm"
    assert report["cases"][0]["reason_code"] == "direct_unresolved"
    assert report["missed_shortcuts"] == 1
    assert report["failed_case_ids"] == []


@pytest.mark.asyncio
async def test_synthetic_corpus_has_no_unsafe_actions_and_preserves_explicit_settings():
    module = evaluator()
    cases = module.load_cases(CORPUS)
    assert len(cases) >= 60
    groups = {}
    for value in cases:
        groups[value["group"]] = groups.get(value["group"], 0) + 1
    assert all(size >= 5 for size in groups.values())
    report = await module.evaluate_cases(cases)
    assert report["failed_case_ids"] == []
    assert report["forbidden_side_effects"] == 0
    assert report["isolation_violations"] == 0
    assert report["explicit_setting_violations"] == 0
    assert report["explicit_setting_cases"] >= 10
    assert report["latency_ms"]["scope"] == "local_routing_with_fixture_boundaries"


def test_cli_does_not_read_environment_or_prompt_files(tmp_path):
    output = tmp_path / "evaluation.json"
    # A child must ignore inherited credentials, .env and local prompt overrides.
    (tmp_path / ".env").write_text("THIS_IS_NOT_A_REAL_SECRET=synthetic-sentinel\n", encoding="utf-8")
    (tmp_path / "prompts.json").write_text("malformed synthetic prompt override", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "evaluate_intent_routing.py"), "--output", str(output)],
        cwd=tmp_path,
        env={**os.environ, "WEATHER_API_KEY": "synthetic-inherited-key", "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["isolation_violations"] == 0
    assert "synthetic-inherited-key" not in output.read_text(encoding="utf-8")
    assert report["case_count"] >= 60

"""Direct OpenAI environment parsing and deployment forwarding contracts."""

from pathlib import Path

import pytest

from app.config import load_settings


@pytest.mark.parametrize(
    ("models", "budget"),
    [
        (("openai-direct/gpt-6.1-terra",), 20.0),
        (("openai-direct/gpt-6.1-terra", "openai-direct/gpt-6-luna"), 20.0),
        (("openai-direct/gpt-6.1-terra", "gemini-3.6-flash"), 20.0),
        (("gemini-3.6-flash", "openai-direct/gpt-6.1-terra"), None),
        (("gemini-3.6-flash",), None),
    ],
)
def test_direct_primary_has_one_shared_first_text_budget(models, budget):
    from app.providers.stream_types import GenerationRequest, PromptRole, PromptTurn, TextPart

    turns = (PromptTurn(PromptRole.USER, (TextPart("Привет"),)),)
    assert GenerationRequest(models=models, turns=turns).first_text_timeout_seconds == budget


def test_openai_credentials_are_optional_and_pool_takes_precedence(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert load_settings().OPENAI_API_KEYS == []
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-single")
    assert load_settings().OPENAI_API_KEYS == ["synthetic-single"]
    monkeypatch.setenv("OPENAI_API_KEYS", '"synthetic-one, synthetic-two"')
    assert load_settings().OPENAI_API_KEYS == ["synthetic-one", "synthetic-two"]


def test_openai_roles_are_loaded_at_reader_without_replacing_gemini_roles(monkeypatch):
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("OPENAI_QNA_MODEL", "openai-direct/gpt-6.1-terra")
    monkeypatch.setenv("OPENAI_INLINE_MODEL", "gpt-6-luna")
    loaded = load_settings()
    assert loaded.OPENAI_DEFAULT_MODEL == "openai-direct/gpt-6.1-sol"
    assert loaded.OPENAI_QNA_MODEL == "openai-direct/gpt-6.1-terra"
    assert loaded.OPENAI_INLINE_MODEL == "openai-direct/gpt-6-luna"
    assert loaded.DEFAULT_MODEL.startswith("gemini-")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("none", []),
        (
            "openai-direct/gpt-6.1-sol,openai-direct/gpt-6-luna,openai-direct/gpt-6.1-sol",
            ["openai-direct/gpt-6.1-sol", "openai-direct/gpt-6-luna"],
        ),
        (
            "vendor/model,gemini-3.6-flash,openai-direct/gpt-image-2,openai-direct/gpt-6-luna",
            ["openai-direct/gpt-6-luna"],
        ),
    ],
)
def test_openai_selector_catalog_preserves_exact_order_and_intentional_empty(monkeypatch, value, expected):
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", value)
    assert load_settings().OPENAI_AVAILABLE_MODELS == expected


def test_deployment_forwards_direct_openai_configuration_to_container():
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    ssh_envs = workflow.split("envs:", 1)[1].splitlines()[0]
    for name in (
        "OPENAI_API_KEYS",
        "OPENAI_AVAILABLE_MODELS",
        "OPENAI_DEFAULT_MODEL",
        "OPENAI_QNA_MODEL",
        "OPENAI_INLINE_MODEL",
    ):
        assert f"{name}: ${{{{ secrets.{name} }}}}" in workflow
        assert name in ssh_envs.split(", ")
        assert f'-e {name}="${name}"' in workflow

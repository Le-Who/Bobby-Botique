"""Regression coverage for runtime routing, quota scope and research prompts."""

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.process_policies import ProcessSpec, validate_policy
from app.runtime_settings import models, store


@pytest.mark.parametrize("process", ["chat", "summary", "tarot.chat", "roles.generate"])
@pytest.mark.parametrize("model", ["vhr/gpt_image_2", "img/new_image", "or/google/lyria-3-clip-preview"])
def test_text_process_rejects_specialized_freetheai_media(process, model):
    with pytest.raises(ValueError, match="тип|type"):
        validate_policy(process, {"models": [model]})


def test_text_process_keeps_unknown_compatible_model_ids():
    assert validate_policy("chat", {"models": ["vendor/new-text-model"]})["models"] == ["vendor/new-text-model"]


@pytest.mark.parametrize(
    "model",
    ["gemini-embedding-2-preview", "gemini-3.1-flash-image", "gemini-3.1-flash-live-preview", "gemini-new/model"],
)
def test_limit_rejects_models_outside_shared_gemini_quota(model):
    with pytest.raises(ValueError):
        models.validate_limit(model, 20)


@pytest.mark.parametrize("model", ["gemini-next-text", "gemini-next-tts"])
def test_limit_accepts_unknown_compatible_gemini_and_tts(model):
    assert models.validate_limit(model, None) is None
    assert models.validate_limit(model, 20) == 20


async def test_limits_include_configured_process_models_and_describe_only_shared_quota(monkeypatch):
    from app import config, process_policies
    from app.repos import keys

    configured = SimpleNamespace(
        DAILY_LIMITS={"gemini-catalog-text": 100, "gemini-3.1-flash-image": 50},
        AVAILABLE_MODELS=["gemini-catalog-text", "gemini-embedding-2-preview"],
    )
    monkeypatch.setattr(config, "settings", configured)
    monkeypatch.setattr(keys, "settings", configured)
    monkeypatch.setattr(keys, "db_query", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        process_policies,
        "PROCESSES",
        {
            "chat": ProcessSpec("Chat", "Chat", "", "gemini-baseline-text"),
            "image.gemini": ProcessSpec("Image", "Image", "", "gemini-3.1-flash-image", ("image_output",)),
            "embedding": ProcessSpec("Embedding", "Memory", "", "gemini-embedding-2-preview", ("embedding",)),
            "live": ProcessSpec("Live", "Audio", "", "gemini-3.1-flash-live-preview", ("live",)),
        },
    )
    monkeypatch.setattr(process_policies, "baseline_models", AsyncMock(return_value=("gemini-baseline-text",)))
    monkeypatch.setattr(keys.db_manager, "_model_config_cache", {"gemini-catalog-text": 100, "gemini-next-text": None})
    snapshot = store.SettingsSnapshot(
        5,
        MappingProxyType(
            {
                "process:chat": {
                    "models": ["gemini-next-text", "gemini-next-process", "vendor/text"],
                    "strategy": "sequential",
                },
                "process:image.gemini": {"models": ["gemini-next-image"], "strategy": "sequential"},
                "model_limit:gemini-next-text": 25,
            }
        ),
    )
    rows = {row["model"]: row for row in await models.list_limits(snapshot=snapshot)}
    assert set(rows) == {"gemini-catalog-text", "gemini-next-text", "gemini-next-process"}
    assert rows["gemini-next-text"]["limit"] == 25
    assert rows["gemini-next-text"]["source"] == "admin"
    assert rows["gemini-next-text"]["scope"] == "gemini_key_rpd"
    assert "IMAGE_GEN_RPD_PER_KEY" in rows["gemini-next-text"]["note"]


async def test_limits_include_actual_research_setting_outside_catalog(monkeypatch):
    from app import config, process_policies
    from app.repos import keys

    configured = SimpleNamespace(DAILY_LIMITS={}, AVAILABLE_MODELS=[], RESEARCH_MODEL="gemini-new-research")
    monkeypatch.setattr(config, "settings", configured)
    monkeypatch.setattr(keys, "settings", configured)
    monkeypatch.setattr(process_policies, "PROCESSES", {"research": process_policies.PROCESSES["research"]})
    monkeypatch.setattr(keys.db_manager, "_model_config_cache", {"gemini-new-research": 70})
    snapshot = store.SettingsSnapshot(1, MappingProxyType({}))
    rows = await models.list_limits(snapshot=snapshot)
    assert [(row["model"], row["limit"]) for row in rows] == [("gemini-new-research", 70)]


async def test_research_synthesis_edits_reach_sdk_and_preserve_query_history_and_evidence(monkeypatch):
    from app import prompt_registry
    from app.core import agentic
    from app.core.research_budget import ResearchBudget
    from app.runtime_settings import prompts
    from app.runtime_settings.lifecycle import load_controlled_prompts, runtime_settings_scope

    load_controlled_prompts()
    registry = prompt_registry.PromptRegistry()
    monkeypatch.setattr(prompt_registry, "_registry_instance", registry)
    snapshot = store.SettingsSnapshot(
        7,
        MappingProxyType(
            {
                "prompt:research.synthesis.system": "Edited synthesis for {query}; pages={max_pages}",
                "prompt:research.synthesis.request": "Edited conclusion for {query}",
            }
        ),
    )
    captured = {}

    async def generate(**kwargs):
        if kwargs["config"].tools:
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name="read_page", args={"url": "https://example.com/gap"}
                                    )
                                )
                            ],
                        )
                    )
                ]
            )
        captured["system"] = kwargs["config"].system_instruction
        captured["contents"] = [content.model_dump() for content in kwargs["contents"]]
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text="Final")]))
            ]
        )

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(agentic, "get_cached_genai_client", lambda key: client)
    monkeypatch.setattr(agentic, "read_url", AsyncMock(return_value="Evidence with {query} stays literal"))
    monkeypatch.setattr(agentic, "_get_cached_page", lambda url: None)
    monkeypatch.setattr(agentic, "_set_cached_page", lambda url, text: None)
    agent = agentic.AgenticSearch("gemini-next-text", "test-key", budget=ResearchBudget())
    agent.max_iterations = 1
    async with runtime_settings_scope(snapshot):
        catalog = {row["id"]: row for row in await prompts.list_prompts()}
        assert catalog["research.synthesis.system"]["source"] == "override"
        result = await agent.run(
            "Literal {max_pages}", AsyncMock(), history=[{"role": "user", "parts": ["History {query}"]}]
        )
    assert result.answer == "Final"
    assert captured["system"] == "Edited synthesis for Literal {max_pages}; pages=3"
    payload = captured["contents"]
    assert payload[0]["parts"][0]["text"] == "History {query}"
    assert payload[1]["parts"][0]["text"] == "Literal {max_pages}"
    assert payload[-1]["parts"][0]["text"] == "Edited conclusion for Literal {max_pages}"
    evidence = [part["function_response"] for row in payload for part in row["parts"] if part.get("function_response")]
    assert evidence[0]["response"]["content"] == "Evidence with {query} stays literal"


def test_research_synthesis_prompt_requires_payload_variables():
    from app.prompt_registry import validate_prompt_text
    from app.runtime_settings.lifecycle import load_controlled_prompts

    load_controlled_prompts()
    with pytest.raises(ValueError, match="placeholders"):
        validate_prompt_text("research.synthesis.system", "Edited without query or max pages")

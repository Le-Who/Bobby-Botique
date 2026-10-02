"""Offline contracts for prompt examples and the payloads sent to consumers.

These checks do not evaluate a model's instruction following or factual accuracy.
"""

import json
import re

import pytest

from app.prompt_registry import PromptRegistry, render_prompt_text, validate_prompt_text
from app.runtime_settings.lifecycle import load_controlled_prompts


@pytest.fixture
def catalog():
    load_controlled_prompts()
    return PromptRegistry().prompt_catalog()


def test_every_registered_baseline_renders_without_reinterpreting_inserted_data(catalog):
    """A template edit must not drop fields, corrupt braces or re-expand user data."""
    for entry in catalog:
        baseline = entry["baseline"]
        validate_prompt_text(entry["id"], baseline)
        values = {field: f'<{field}> {{"nested": {{"text": "{{unknown}}"}}}}' for field in entry["variables"]}
        rendered = render_prompt_text(baseline, **values)
        for value in values.values():
            assert value in rendered, entry["id"]


@pytest.mark.parametrize("image_count", [1, 3])
def test_vision_provider_prompt_matches_input_cardinality(image_count):
    """A single photo must not receive album instructions, or vice versa."""
    from app.handlers.ai_photo import _build_vision_prompt
    from app.prompt_registry import prompt_scope

    with prompt_scope(overrides={}, revision=0):
        prompt = _build_vision_prompt("Что здесь важно? {user_text}", image_count=image_count)
    assert "Что здесь важно? {user_text}" in prompt
    assert ("каждое изображение" in prompt.lower()) == (image_count > 1)
    assert ("группу изображений" in prompt.lower()) == (image_count > 1)


def test_role_json_examples_are_accepted_by_role_consumer():
    """Every fenced role example must be valid JSON with consumer-required fields."""
    from app.utils.json_utils import extract_json_object

    prompt = PromptRegistry().get_task_prompt("prompt_engineer")
    examples = re.findall(r"```(?:json)?\s*\n(.*?)\n```", prompt, re.DOTALL)
    assert examples
    for example in examples:
        payload = json.loads(example)
        extracted = extract_json_object(example)
        assert extracted is not None
        assert extracted["prompt"] == payload["system_prompt"]
        assert isinstance(payload["capabilities"], list)
        assert isinstance(payload["constraints"], list)
        assert isinstance(payload["style"], list)
        assert isinstance(payload["examples"], list)


@pytest.mark.parametrize("prompt_id", ["trivia.main", "trivia.super"])
def test_trivia_example_matches_question_parser_and_answer_identity(catalog, prompt_id):
    """The illustrated answer index and fact identity must refer to the same option."""
    from app.games.daily_trivia import shuffle_options_and_update_correct_index
    from app.games.daily_trivia_authoring import _validate_question

    prompt = next(entry["baseline"] for entry in catalog if entry["id"] == prompt_id)
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{") :])
    question = shuffle_options_and_update_correct_index([example])[0]
    _validate_question(question)
    assert question.identity.answer == question.options[question.correct_index].casefold()


def test_semantic_duplicate_example_matches_structured_verdict(catalog):
    from app.games.daily_trivia_authoring import SemanticDuplicateJudgement

    prompt = next(entry["baseline"] for entry in catalog if entry["id"] == "trivia.deduplicate")
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{") :])
    verdict = SemanticDuplicateJudgement.model_validate(example)
    assert isinstance(verdict.is_duplicate, bool)


def test_memory_relevance_example_is_a_json_array_of_indexed_booleans(catalog):
    prompt = next(entry["baseline"] for entry in catalog if entry["id"] == "memory.relevance")
    example, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("[") :])
    assert example
    assert all(type(item["index"]) is int and type(item["relevant"]) is bool for item in example)

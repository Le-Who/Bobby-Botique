"""Explicit specialized plans use their exact model order; defaults retain legacy calls."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.process_policies import ResolvedPolicy


def test_domain_prompts_use_live_catalog_text_without_expanding_chart_content():
    from app import prompt_registry
    from app import tarot_daily as tarot
    from app.games import daily_trivia, daily_trivia_authoring  # noqa: F401 — registers domain prompt templates
    from app.natal import llm
    from app.natal.models import ChartData, InputQuality, TimePrecision

    registry = prompt_registry.get_registry()
    ids = {row["id"] for row in registry.prompt_catalog()}
    assert {
        "trivia.main",
        "trivia.super",
        "trivia.deduplicate",
        "trivia.audit",
        "natal.interpretation",
        "natal.repair",
        "tarot.daily",
    } <= ids
    edited = prompt_registry.get_prompt_text("tarot.daily").replace("Ты — мистический таролог", "Ты — редактор")
    natal_edited = prompt_registry.get_prompt_text("natal.interpretation").replace("Ты пишешь", "Ты редактируешь")
    registry.apply_overrides({"tarot.daily": edited, "natal.interpretation": natal_edited}, revision=10_000)
    try:
        assert tarot._build_daily_system_instruction("Карта: Маг").startswith("Ты — редактор")
        chart = ChartData(
            input_quality=InputQuality(
                time_precision=TimePrecision.UNKNOWN,
                houses_available=False,
                angles_available=False,
            ),
            planets=[],
            aspects=[],
        )
        text = llm.build_interpretation_prompt(chart, "ru", "{chart_json}")
        assert text.startswith("Ты редактируешь")
        assert "Фокус: {chart_json}." in text
    finally:
        registry.apply_overrides({}, revision=10_001)
        prompt_registry.reset_registry()


@pytest.mark.asyncio
async def test_trivia_generation_uses_only_explicit_models(monkeypatch):
    from app.games import daily_trivia as game

    plans = []

    async def resolve(process_id, baseline):
        assert process_id == "daily.trivia"
        assert len(baseline) > 1
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 8)

    class Router:
        async def execute_gemini_model_plan(self, models, history, **kwargs):
            plans.append(tuple(models))
            return []

    monkeypatch.setattr(game, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(game.repo, "get_recent_bank_facts", AsyncMock(return_value=[]))
    await game.generate_question_lane(date(2026, 9, 30), lane="main", model_name="gemini-old", router=Router())
    assert plans == [("gemini-admin-a", "gemini-admin-b")]


@pytest.mark.asyncio
async def test_trivia_semantic_routes_use_distinct_ordered_plans(monkeypatch):
    from app.games import daily_trivia_authoring as authoring
    from app.games.trivia_similarity import FactIdentity

    calls = []

    async def resolve(process_id, baseline):
        calls.append((process_id, baseline))
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 4)

    class Router:
        async def execute_gemini_model_plan(self, models, history, *, parse_response, **kwargs):
            assert tuple(models) == ("gemini-admin-a", "gemini-admin-b")
            if "Факт A" in history[0]["parts"][0]["text"]:
                return parse_response('{"is_duplicate":false,"confidence":0.1,"reason":"different"}')
            return parse_response('{"conflicts":[]}')

    monkeypatch.setattr(authoring, "resolve_process", resolve, raising=False)
    judge = authoring.build_semantic_judge(router=Router(), model_name="gemini-base")
    assert await judge("alpha", "beta") == (False, 0.1, "different")
    fact = authoring.BankFact(FactIdentity.create(subject="A", relation="B", answer="C"), "Question")
    assert await authoring.audit_semantic_bank([fact], [fact], router=Router(), model_name="gemini-base") == []
    assert calls == [
        ("daily.trivia.deduplicate", ("gemini-base",)),
        ("daily.trivia.audit", ("gemini-base",)),
    ]


@pytest.mark.asyncio
async def test_natal_interpretation_and_repair_keep_explicit_plan(monkeypatch):
    from app.natal import llm

    calls = []
    chart = SimpleNamespace()
    valid_section = llm.ReportSection(id="section-summary", title="Summary", body_markdown="Text")

    async def resolve(process_id, baseline):
        assert process_id == "natal"
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 3)

    class Router:
        async def execute_gemini_model_plan(self, models, history, **kwargs):
            calls.append(tuple(models))
            return "## section-summary | Summary\nText"

    monkeypatch.setattr(llm, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(llm, "build_interpretation_prompt", lambda *args: "prompt")
    monkeypatch.setattr(llm, "_build_interpretation_repair_prompt", lambda *args: "repair")
    monkeypatch.setattr(llm, "_parse_sections", lambda text: [valid_section])
    monkeypatch.setattr(llm, "_sections_contradict_calculated_signs", lambda *args: False)
    monkeypatch.setattr(llm, "_sections_need_quality_repair", lambda *args: len(calls) == 1)
    monkeypatch.setattr(llm, "sanitize_user_facing_sections", lambda sections: sections)
    monkeypatch.setattr("app.providers.get_provider_router", lambda: Router())

    result = await llm.generate_interpretation(chart, user_id=1, chat_id=2)
    assert result == [valid_section]
    assert calls == [("gemini-admin-a", "gemini-admin-b")] * 2


@pytest.mark.asyncio
async def test_tarot_preparation_uses_explicit_plan_for_each_card(monkeypatch):
    from app import tarot_daily as tarot

    plans = []

    async def resolve(process_id, baseline):
        assert process_id == "tarot.daily"
        assert baseline == (tarot.TAROT_DAILY_MODEL,)
        return ResolvedPolicy(("gemini-admin-a", "gemini-admin-b"), "sequential", True, 9)

    class Router:
        async def execute_gemini_model_plan(self, models, history, **kwargs):
            plans.append(tuple(models))
            return "Prepared reading"

    monkeypatch.setattr(tarot, "resolve_process", resolve, raising=False)
    monkeypatch.setattr(
        tarot,
        "iter_daily_card_variants",
        lambda: [
            {
                "name": "Magician",
                "orientation": "upright",
                "context": "meaning",
                "label": "Magician (upright)",
            }
        ],
    )
    monkeypatch.setattr(tarot, "get_provider_router", lambda: Router())
    monkeypatch.setattr(tarot, "count_gemini_keys", AsyncMock(return_value=2))
    monkeypatch.setattr(tarot, "get_prepared_daily_reading", AsyncMock(return_value=None))
    saved = AsyncMock()
    monkeypatch.setattr(tarot, "upsert_prepared_daily_reading", saved)
    monkeypatch.setattr(tarot, "_acquire_redis_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(tarot, "_release_redis_lock", AsyncMock())

    result = await tarot.prepare_daily_readings(target_date=date(2026, 9, 30), sleep=AsyncMock())
    assert result.generated == 1
    assert plans == [("gemini-admin-a", "gemini-admin-b")]
    assert saved.await_args.kwargs["body_markdown"] == "Prepared reading"

"""Interpretation uses local facts and never receives birth input."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.natal.compatibility import build_birth_date_compatibility, parse_compatibility_query


@pytest.fixture
def provider(monkeypatch):
    from app.process_policies import ResolvedPolicy

    router = SimpleNamespace(
        get_response=AsyncMock(return_value=("**Ваша пара**\nТепло встречается со свободой.", 120))
    )
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)
    monkeypatch.setattr(
        "app.process_policies.resolve_process", AsyncMock(return_value=ResolvedPolicy(("gemini-3.1-flash-lite",)))
    )
    return router


@pytest.mark.asyncio
async def test_interpretation_receives_derived_facts_and_preserves_original_tarot_context(provider):
    from app.natal.compatibility_interpretation import interpret_compatibility

    reading = build_birth_date_compatibility(
        parse_compatibility_query("совм рак скорпион"), date(2003, 6, 30), date(1997, 11, 9)
    )
    result = await interpret_compatibility(reading, user_id=777, lang="ru")

    request = provider.get_response.await_args.kwargs
    facts = request["history"][0]["parts"][0]
    assert "Солнце: Рак" in facts and "Солнце: Скорпион" in facts
    assert "2003" not in facts and "1997" not in facts
    assert request["user_id"] == request["chat_id"] == 777
    assert "<b>Ваша пара</b>" in result.html
    assert "Тепло встречается со свободой." in result.html
    assert "В этом разборе указаны только даты" in result.html
    assert result.tarot_context == reading.tarot_context


@pytest.mark.asyncio
async def test_model_cannot_drop_per_partner_uncertainty(provider):
    from app.natal.compatibility_input import parse_pair_input
    from app.natal.compatibility_interpretation import interpret_compatibility
    from app.natal.compatibility_reading import build_pair_reading

    value = parse_pair_input(
        {
            "pair": "compat_n0_n6",
            "first": {
                "birth_date": "1995-06-15",
                "time_precision": "exact",
                "birth_time": "12:30",
                "country_code": "UA",
                "city_geoname_id": "703448",
            },
            "second": {"birth_date": "1989-10-20", "time_precision": "unknown", "place_unknown": True},
        }
    )
    result = await interpret_compatibility(await build_pair_reading(value), user_id=777, lang="ru")

    facts = provider.get_response.await_args.kwargs["history"][0]["parts"][0]
    assert "Асцендент:" in facts
    assert "Партнёр 2: время не указано" in facts
    for raw in ("1995-06-15", "1989-10-20", "12:30", "Kyiv", "Киев", "703448"):
        assert raw not in facts
    assert "Партнёр 2: время не указано" in result.html
    assert "Партнёр 1: время не указано" not in result.html


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "   "])
async def test_empty_interpretation_is_a_failure_without_mechanical_fallback(provider, response):
    from app.natal.compatibility import BirthDateCompatibility
    from app.natal.compatibility_interpretation import interpret_compatibility

    provider.get_response.return_value = (response, 0)
    with pytest.raises(ValueError, match="Empty compatibility interpretation"):
        await interpret_compatibility(BirthDateCompatibility("Local facts", "Derived context"), user_id=777, lang="en")


@pytest.mark.asyncio
async def test_provider_failure_propagates_without_returning_template(provider):
    from app.natal.compatibility import BirthDateCompatibility
    from app.natal.compatibility_interpretation import interpret_compatibility

    provider.get_response.side_effect = TimeoutError("provider unavailable")
    with pytest.raises(TimeoutError):
        await interpret_compatibility(BirthDateCompatibility("Local facts", "Derived context"), user_id=777, lang="en")


@pytest.mark.asyncio
async def test_tagged_provider_error_is_not_published_as_a_reading(provider):
    from app.errors import ErrorCode, tag_error
    from app.natal.compatibility import BirthDateCompatibility
    from app.natal.compatibility_interpretation import interpret_compatibility

    provider.get_response.return_value = (tag_error(ErrorCode.RATE_LIMIT, "Try later"), None)
    with pytest.raises(ValueError, match="provider failed"):
        await interpret_compatibility(BirthDateCompatibility("Local facts", "Derived context"), user_id=777, lang="en")

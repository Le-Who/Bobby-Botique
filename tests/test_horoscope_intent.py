# tests/test_horoscope_intent.py
"""Unit tests for the Horoscope direct intent routing."""

import pytest

from app.intent_router import (
    _DATE_POSLEZAVTRA_RE,
    _DATE_SEGODNYA_RE,
    _DATE_VCHERA_RE,
    _DATE_ZAVTRA_RE,
    _HOROSCOPE_PATTERNS,
    _ZODIAC_MAPPING,
    _build_horoscope_system_instruction,
    _format_horoscope_response,
    _missing_horoscope_sign_guide,
    try_direct_intent,
)


def test_horoscope_intent_matching():
    """Verify that general horoscope words trigger the intent pattern."""
    assert _HOROSCOPE_PATTERNS.search("гороскоп") is not None
    assert _HOROSCOPE_PATTERNS.search("мой гороскоп на сегодня") is not None
    assert _HOROSCOPE_PATTERNS.search("horoscope for today") is not None
    assert _HOROSCOPE_PATTERNS.search("что там по зодиаку?") is not None
    assert _HOROSCOPE_PATTERNS.search("zodiac signs") is not None
    assert _HOROSCOPE_PATTERNS.search("привет") is None


def test_zodiac_sign_parsing():
    """Verify that all 12 Russian/English zodiac signs in various grammatical cases are correctly mapped."""
    # Aries
    assert _ZODIAC_MAPPING["aries"].search("овен") is not None
    assert _ZODIAC_MAPPING["aries"].search("овна") is not None
    assert _ZODIAC_MAPPING["aries"].search("овну") is not None

    # Taurus
    assert _ZODIAC_MAPPING["taurus"].search("телец") is not None
    assert _ZODIAC_MAPPING["taurus"].search("тельца") is not None
    assert _ZODIAC_MAPPING["taurus"].search("тельцу") is not None

    # Gemini
    assert _ZODIAC_MAPPING["gemini"].search("близнецы") is not None
    assert _ZODIAC_MAPPING["gemini"].search("близнецов") is not None
    assert _ZODIAC_MAPPING["gemini"].search("близнецам") is not None

    # Cancer
    assert _ZODIAC_MAPPING["cancer"].search("рак") is not None
    assert _ZODIAC_MAPPING["cancer"].search("рака") is not None
    assert _ZODIAC_MAPPING["cancer"].search("раку") is not None

    # Leo
    assert _ZODIAC_MAPPING["leo"].search("лев") is not None
    assert _ZODIAC_MAPPING["leo"].search("льва") is not None
    assert _ZODIAC_MAPPING["leo"].search("львом") is not None

    # Virgo
    assert _ZODIAC_MAPPING["virgo"].search("дева") is not None
    assert _ZODIAC_MAPPING["virgo"].search("девы") is not None
    assert _ZODIAC_MAPPING["virgo"].search("деве") is not None

    # Libra
    assert _ZODIAC_MAPPING["libra"].search("весы") is not None
    assert _ZODIAC_MAPPING["libra"].search("весов") is not None
    assert _ZODIAC_MAPPING["libra"].search("весами") is not None

    # Scorpio
    assert _ZODIAC_MAPPING["scorpio"].search("скорпион") is not None
    assert _ZODIAC_MAPPING["scorpio"].search("скорпиона") is not None

    # Sagittarius
    assert _ZODIAC_MAPPING["sagittarius"].search("стрелец") is not None
    assert _ZODIAC_MAPPING["sagittarius"].search("стрельца") is not None

    # Capricorn
    assert _ZODIAC_MAPPING["capricorn"].search("козерог") is not None
    assert _ZODIAC_MAPPING["capricorn"].search("козерога") is not None

    # Aquarius
    assert _ZODIAC_MAPPING["aquarius"].search("водолей") is not None
    assert _ZODIAC_MAPPING["aquarius"].search("водолея") is not None

    # Pisces
    assert _ZODIAC_MAPPING["pisces"].search("рыбы") is not None
    assert _ZODIAC_MAPPING["pisces"].search("рыбе") is not None
    assert _ZODIAC_MAPPING["pisces"].search("рыб") is not None


def test_date_target_parsing():
    """Verify that relative date terms match the expected patterns."""
    assert _DATE_SEGODNYA_RE.search("сегодня") is not None
    assert _DATE_SEGODNYA_RE.search("today") is not None

    assert _DATE_ZAVTRA_RE.search("завтра") is not None
    assert _DATE_ZAVTRA_RE.search("tomorrow") is not None

    assert _DATE_POSLEZAVTRA_RE.search("послезавтра") is not None

    assert _DATE_VCHERA_RE.search("вчера") is not None
    assert _DATE_VCHERA_RE.search("yesterday") is not None


def test_horoscope_prompt_uses_structured_safe_contract():
    prompt = _build_horoscope_system_instruction(
        user_text="гороскоп овен на завтра",
        signs_str="Овен ♈",
        day_ru="завтра",
        astro_context="Луна в Тельце; Меркурий ретроградный",
    )

    assert "<role>" in prompt
    assert "<constraints>" in prompt
    assert "<output_format>" in prompt
    assert "без фатализма" in prompt.lower()
    assert "медицинских, юридических или финансовых указаний" in prompt
    assert "Луна в Тельце" in prompt


def test_horoscope_response_footer_is_product_facing_not_provider_facing():
    text = _format_horoscope_response(
        sign_displays=["Овен ♈"],
        day_ru="сегодня",
        body_text="Сегодня полезно выбирать один главный фокус.",
    )

    assert text.startswith("🔮 **Гороскоп: Овен ♈ (сегодня)**")
    assert "_Астро-данные: текущие транзиты" in text
    assert "Gemini" not in text


def test_missing_horoscope_sign_guide_invites_subscribe_settings():
    text = _missing_horoscope_sign_guide()

    assert "укажите знак зодиака" in text.lower()
    assert "/horoscope_settings" in text
    assert "гороскоп овен на сегодня" in text


@pytest.mark.asyncio
async def test_direct_intent_horoscope_no_sign(monkeypatch):
    """Verify that a horoscope query with no sign returns the zodiac signs guide."""
    res = await try_direct_intent("хочу узнать гороскоп")
    assert res is not None
    assert res.handled is True
    assert "🔮 **Персональный Гороскоп**" in res.text
    assert "Пожалуйста, укажите знак зодиака" in res.text


@pytest.mark.asyncio
async def test_horoscope_generation_uses_typed_router_events(monkeypatch):
    from app.intent_router import _handle_horoscope
    from app.providers.stream_types import (
        FinishReason,
        GroundingReport,
        ProviderKind,
        RouteUsed,
        StreamCompleted,
        TextDelta,
        TokenUsage,
    )

    class Router:
        requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield TextDelta("Сегодня стоит действовать спокойно.")
            yield StreamCompleted(
                finish_reason=FinishReason.from_raw("STOP"),
                usage=TokenUsage(total=8),
                grounding=GroundingReport(),
                route=RouteUsed(
                    provider=ProviderKind.GEMINI,
                    requested_model=request.models[0],
                    actual_model=request.models[0],
                ),
            )

    router = Router()
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)
    monkeypatch.setattr("app.astro.get_astro_context", lambda _date: "astro")

    result = await _handle_horoscope("гороскоп овен на сегодня")

    assert result is not None
    assert "Сегодня стоит действовать спокойно" in result.text
    assert len(router.requests) == 1
    assert router.requests[0].allow_deferred is False

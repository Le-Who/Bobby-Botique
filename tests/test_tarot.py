import pytest

from app.tarot import SPREAD_POSITIONS, SpreadType, draw_cards, get_fortune_cookie, get_tarot_context


def test_spread_types():
    assert SpreadType.CLASSIC.value == "tarot"
    assert SpreadType.DAILY.value == "tarot_daily"
    assert SpreadType.YES_NO.value == "tarot_yesno"
    assert SpreadType.LOVE.value == "tarot_love"
    assert SpreadType.CELTIC.value == "tarot_celtic"
    assert SpreadType.FORTUNE.value == "tarot_fortune"

def test_spread_positions():
    assert len(SPREAD_POSITIONS[SpreadType.CLASSIC]) == 3
    assert len(SPREAD_POSITIONS[SpreadType.DAILY]) == 1
    assert len(SPREAD_POSITIONS[SpreadType.YES_NO]) == 1
    assert len(SPREAD_POSITIONS[SpreadType.LOVE]) == 5
    assert len(SPREAD_POSITIONS[SpreadType.CELTIC]) == 6
    assert len(SPREAD_POSITIONS[SpreadType.FORTUNE]) == 0

def test_draw_cards():
    cards = draw_cards(3)
    assert len(cards) == 3
    for card in cards:
        assert "name" in card
        assert "orientation" in card
        assert "is_reversed" in card
        assert "keywords" in card
        assert "meanings" in card
        assert card["orientation"] in ("Прямая", "Перевернутая")

def test_get_tarot_context_classic():
    context, names = get_tarot_context(SpreadType.CLASSIC)
    assert len(names) == 3
    assert "Позиция «Прошлое»" in context
    assert "Позиция «Настоящее»" in context
    assert "Позиция «Будущее»" in context

def test_get_tarot_context_legacy_int():
    # Backward compatibility with integers
    context, names = get_tarot_context(3)
    assert len(names) == 3
    assert "Позиция «Прошлое»" in context
    
    context_2, names_2 = get_tarot_context(2)
    assert len(names_2) == 2
    assert "Позиция «Карта 1»" in context_2

def test_get_tarot_context_daily():
    context, names = get_tarot_context(SpreadType.DAILY)
    assert len(names) == 1
    assert "Позиция «Карта дня»" in context

def test_get_fortune_cookie():
    res = get_fortune_cookie()
    assert res is not None
    fortune, name = res
    assert isinstance(fortune, str)
    assert len(fortune) > 0
    assert isinstance(name, str)
    assert len(name) > 0

# These will test functions from app.handlers.inline which are NOT yet implemented or imported in test.
# They should fail or raise ImportError/AttributeError until we apply the patch, verifying the RED phase.
def test_build_fortune_cookie_html():
    from app.handlers.inline import _build_fortune_cookie_html
    html = _build_fortune_cookie_html()
    assert "⚡ <b>Предсказание</b>" in html
    assert "<i>«" in html

def test_build_tarot_system_prompt_daily():
    from app.handlers.inline import _build_tarot_system_prompt
    prompt = _build_tarot_system_prompt(SpreadType.DAILY, "ctx data", "question")
    assert "карта дня" in prompt.lower()
    assert "совет и энергия на сегодня" in prompt.lower()

def test_build_tarot_system_prompt_yesno():
    from app.handlers.inline import _build_tarot_system_prompt
    prompt_upright = _build_tarot_system_prompt(SpreadType.YES_NO, "Прямая", "question")
    assert "оракул таро" in prompt_upright.lower()
    assert "да" in prompt_upright.lower()

    prompt_reversed = _build_tarot_system_prompt(SpreadType.YES_NO, "Перевернутая", "question")
    assert "нет" in prompt_reversed.lower()

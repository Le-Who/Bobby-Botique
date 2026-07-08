from app.natal.intent import is_natal_chart_request


def test_detects_explicit_natal_chart_request():
    assert is_natal_chart_request("сделай мне натальную карту")
    assert is_natal_chart_request("рассчитай birth chart")
    assert is_natal_chart_request("посчитай натальную карту")
    assert is_natal_chart_request("создай расчет наталки")
    assert is_natal_chart_request("составь натальную карту")
    assert is_natal_chart_request("построй карту рождения")
    assert is_natal_chart_request("/натальная")
    assert is_natal_chart_request("/карта")


def test_does_not_match_generic_horoscope():
    assert not is_natal_chart_request("гороскоп на сегодня для овна")
    assert not is_natal_chart_request("что значит мой знак зодиака")
    assert not is_natal_chart_request("покажи карту метро")

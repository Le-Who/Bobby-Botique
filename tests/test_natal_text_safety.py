from app.natal.text_safety import strip_user_facing_blocked_notes


def test_strip_user_facing_blocked_notes_keeps_style_language_for_llm_repair():
    markdown = (
        "Астрологическая сетка указывает, что это ядро проецируется на сферу ваших личных ресурсов. "
        "Слово натив здесь тоже не технический дисклеймер, даже если такой стиль должен переписать LLM.\n\n"
        "Полезная часть текста должна дойти до пользователя без грубого постфактум-вырезания."
    )

    assert strip_user_facing_blocked_notes(markdown) == markdown


def test_strip_user_facing_blocked_notes_removes_only_explicit_technical_notes():
    markdown = (
        "Техническое примечание: расчетный движок ephem-local использует equal-house и reference validation.\n\n"
        "Солнце в Водолее проще увидеть в жизни так: вам важно понимать, зачем вы участвуете в деле."
    )

    cleaned = strip_user_facing_blocked_notes(markdown)

    assert "ephem-local" not in cleaned
    assert "equal-house" not in cleaned
    assert "reference validation" not in cleaned
    assert "Солнце в Водолее проще увидеть" in cleaned

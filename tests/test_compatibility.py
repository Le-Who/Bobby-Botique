from datetime import date

import pytest

from app.natal import compatibility


@pytest.mark.parametrize(
    "query, signs, genders",
    [
        ("совместимость мужчина скорпион женщина водолей", (7, 10), ("m", "f")),
        ("СОВМ: женщина Водолей и мужчина Скорпион", (10, 7), ("f", "m")),
        ("совм. м скорпион ж водолея", (7, 10), ("m", "f")),
        ("совмест скорпион мужчина водолей женщина", (7, 10), ("m", "f")),
        ("синастрия скорпион скорпион", (7, 7), ("n", "n")),
        ("compatibility man scorpio woman aquarius", (7, 10), ("m", "f")),
        ("compat scorpio and aquarius", (7, 10), ("n", "n")),
        ("совм мужчина лев мужчина стрелец", (4, 8), ("m", "m")),
    ],
)
def test_query_preserves_partner_order_and_repeated_signs(query, signs, genders):
    pair = compatibility.parse_compatibility_query(query)

    assert (pair.first.sign_index, pair.second.sign_index) == signs
    assert (pair.first.gender, pair.second.gender) == genders


@pytest.mark.parametrize(
    "query",
    [
        "совместимость",
        "совм скорпион",
        "совм скорпион водолей рыбы",
        "совм мужчина женщина скорпион водолей",
        "совм скорпион водолей <script>bad</script>",
        "совм 09.11.1997 30.06.2003",
    ],
)
def test_incomplete_or_ambiguous_queries_request_correction(query):
    with pytest.raises(compatibility.CompatibilityQueryError):
        compatibility.parse_compatibility_query(query)


@pytest.mark.parametrize("query", ["hello", "совместимая система", "нарисуй скорпиона", ""])
def test_other_inline_intents_are_not_compatibility(query):
    assert not compatibility.is_compatibility_query(query)


def test_deep_links_roundtrip_pair_without_cache_or_private_data():
    pair = compatibility.parse_compatibility_query("совм м скорпион ж водолей")

    for tarot, prefix in [(False, "compat_"), (True, "compat_t_")]:
        payload = compatibility.compatibility_start_payload(pair, tarot=tarot)
        decoded, mode = compatibility.parse_compatibility_start_payload(payload)
        assert payload.startswith(prefix)
        assert len(payload) <= 64
        assert decoded == pair
        assert mode is tarot


def test_english_query_language_survives_private_deep_link():
    pair = compatibility.parse_compatibility_query("compat man scorpio woman aquarius")
    decoded, _ = compatibility.parse_compatibility_start_payload(compatibility.compatibility_start_payload(pair))

    assert decoded.language == "en"


@pytest.mark.parametrize("payload", ["compat_m12_f1", "compat_x7_f10", "compat_t_m7_f10_extra", "compat_m-1_f2"])
def test_start_payload_rejects_out_of_range_and_malformed_values(payload):
    with pytest.raises(compatibility.CompatibilityQueryError):
        compatibility.parse_compatibility_start_payload(payload)


def test_sign_reading_explains_both_partners_without_fake_precision():
    pair = compatibility.parse_compatibility_query("совм мужчина скорпион женщина водолей")

    body = compatibility.build_sign_compatibility_html(pair, lang="ru")

    assert "Мужчина · Скорпион" in body
    assert "Женщина · Водолей" in body
    assert "довер" in body.lower()
    assert "свобод" in body.lower()
    assert "%" not in body
    assert "по солнечным знакам" in body
    assert len(body) < 4096


@pytest.mark.parametrize(
    "raw, expected",
    [("9.11.1997", date(1997, 11, 9)), ("2003-06-30", date(2003, 6, 30)), ("29.02.2000", date(2000, 2, 29))],
)
def test_birth_dates_are_calendar_validated(raw, expected):
    assert compatibility.parse_compatibility_birth_date(raw, today=date(2026, 10, 2)) == expected


@pytest.mark.parametrize("raw", ["29.02.2001", "09.11.97", "1899-12-31", "2026-10-03", "19971109", "1.1.2000 extra"])
def test_invalid_dates_do_not_advance_private_flow(raw):
    with pytest.raises(ValueError):
        compatibility.parse_compatibility_birth_date(raw, today=date(2026, 10, 2))


def test_birth_date_reading_uses_actual_sun_signs_and_keeps_dates_out_of_tarot_context():
    pair = compatibility.parse_compatibility_query("совм мужчина скорпион женщина водолей")

    reading = compatibility.build_birth_date_compatibility(pair, date(2003, 6, 30), date(1997, 11, 9), lang="ru")

    assert "Рак" in reading.html
    assert "Скорпион" in reading.html
    assert "Луна" in reading.html
    assert "Венера" in reading.html
    assert "Матрицы" in reading.html
    assert "время" in reading.html.lower()
    assert len(reading.html) < 4096
    for private_value in ("2003-06-30", "1997-11-09", "30.06.2003", "09.11.1997"):
        assert private_value not in reading.tarot_context


def test_unknown_time_and_place_do_not_force_single_sun_sign_on_boundary():
    pair = compatibility.parse_compatibility_query("совм овен весы")

    reading = compatibility.build_birth_date_compatibility(pair, date(2023, 3, 21), date(2003, 6, 30), lang="ru")

    assert "Рыбы / Овен" in reading.html
    assert "погранич" in reading.html.lower()

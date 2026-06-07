import pytest

from app.natal.models import TimePrecision
from app.natal.parser import BirthInputParseError, parse_birth_table


def test_parse_exact_time_table_ru():
    parsed = parse_birth_table(
        """
        Дата рождения: 14.02.1995
        Время рождения: точное
        Если точное или примерное: 06:30
        Место рождения: Киев, Украина
        Фокус разбора: отношения
        """
    )

    assert parsed.birth_date == "1995-02-14"
    assert parsed.time_precision == TimePrecision.EXACT
    assert parsed.birth_time == "06:30"
    assert parsed.birth_place == "Киев, Украина"
    assert parsed.focus == "relationships"


def test_parse_unknown_time_table_ru():
    parsed = parse_birth_table(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Место рождения: Kyiv, Ukraine
        """
    )

    assert parsed.time_precision == TimePrecision.UNKNOWN
    assert parsed.birth_time is None
    assert parsed.language == "ru"
    assert parsed.focus == "general"


def test_parse_table_accepts_birth_country_for_local_city_lookup():
    parsed = parse_birth_table(
        """
        Дата рождения: 1995-02-14
        Время рождения: неизвестно
        Страна рождения: Украина
        Место рождения: Одесса
        """
    )

    assert parsed.birth_place_country_code == "UA"
    assert parsed.birth_place == "Одесса"


def test_parse_requires_birth_place():
    with pytest.raises(BirthInputParseError, match="Место рождения"):
        parse_birth_table("Дата рождения: 1995-02-14\nВремя рождения: неизвестно")


def test_parse_exact_time_requires_time_value():
    with pytest.raises(BirthInputParseError, match="точное время"):
        parse_birth_table(
            "Дата рождения: 1995-02-14\n"
            "Время рождения: точное\n"
            "Место рождения: Kyiv"
        )

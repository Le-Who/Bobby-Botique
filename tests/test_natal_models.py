from app.natal.models import BirthInput, TimePrecision


def test_birth_input_exact_time_requires_time_value():
    data = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.EXACT,
        birth_time="06:30",
        birth_place="Kyiv, Ukraine",
        language="ru",
        focus="general",
    )

    assert data.time_precision == TimePrecision.EXACT
    assert data.birth_time == "06:30"


def test_birth_input_unknown_time_accepts_missing_time():
    data = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
        language="ru",
        focus="general",
    )

    assert data.birth_time is None

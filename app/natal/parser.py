from __future__ import annotations

import re
from datetime import date

from app.natal.models import BirthInput, TimePrecision


class BirthInputParseError(ValueError):
    pass


_DATE_RE_DMY = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")
_DATE_RE_ISO = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

_TIME_PRECISION_MAP = {
    "точное": TimePrecision.EXACT,
    "exact": TimePrecision.EXACT,
    "примерное": TimePrecision.APPROXIMATE,
    "approx": TimePrecision.APPROXIMATE,
    "approximate": TimePrecision.APPROXIMATE,
    "диапазон": TimePrecision.RANGE,
    "range": TimePrecision.RANGE,
    "неизвестно": TimePrecision.UNKNOWN,
    "не знаю": TimePrecision.UNKNOWN,
    "unknown": TimePrecision.UNKNOWN,
}

_FOCUS_MAP = {
    "общий": "general",
    "отношения": "relationships",
    "карьера": "career",
    "психология": "psychology",
    "кратко": "brief",
}


def parse_birth_table(text: str) -> BirthInput:
    fields = _parse_key_values(text)

    birth_date = _normalize_date(_get_required(fields, "Дата рождения"))
    precision_raw = _get_required(fields, "Время рождения")
    time_precision = _normalize_time_precision(precision_raw)
    birth_place = _get_required(fields, "Место рождения")
    focus = _normalize_focus(fields.get("фокус разбора", "general"))
    language = fields.get("язык", "ru").strip() or "ru"

    birth_time = None
    range_start = None
    range_end = None

    if time_precision in (TimePrecision.EXACT, TimePrecision.APPROXIMATE):
        birth_time = _extract_time(fields.get("если точное или примерное", ""))
        if not birth_time:
            birth_time = _extract_time(precision_raw)
        if not birth_time and time_precision == TimePrecision.EXACT:
            raise BirthInputParseError("Укажите точное время рождения.")
    elif time_precision == TimePrecision.RANGE:
        range_raw = fields.get("если диапазон", "")
        times = _TIME_RE.findall(range_raw)
        if len(times) >= 2:
            range_start = f"{int(times[0][0]):02d}:{times[0][1]}"
            range_end = f"{int(times[1][0]):02d}:{times[1][1]}"

    return BirthInput(
        birth_date=birth_date,
        time_precision=time_precision,
        birth_time=birth_time,
        birth_time_range_start=range_start,
        birth_time_range_end=range_end,
        birth_place=birth_place,
        language=language,
        focus=focus,
    )


def _parse_key_values(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = key.strip().lower()
        fields[normalized_key] = value.strip()
    return fields


def _get_required(fields: dict[str, str], label: str) -> str:
    value = fields.get(label.lower(), "").strip()
    if not value:
        raise BirthInputParseError(f"Поле обязательно: {label}")
    return value


def _normalize_date(raw: str) -> str:
    dmy = _DATE_RE_DMY.match(raw)
    if dmy:
        day, month, year = (int(part) for part in dmy.groups())
    else:
        iso = _DATE_RE_ISO.match(raw)
        if not iso:
            raise BirthInputParseError("Дата рождения должна быть в формате ДД.ММ.ГГГГ или YYYY-MM-DD.")
        year, month, day = (int(part) for part in iso.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise BirthInputParseError("Дата рождения некорректна.") from exc


def _normalize_time_precision(raw: str) -> TimePrecision:
    value = raw.strip().lower()
    for needle, precision in _TIME_PRECISION_MAP.items():
        if needle in value:
            return precision
    raise BirthInputParseError("Время рождения должно быть: точное, примерное, диапазон или неизвестно.")


def _extract_time(raw: str) -> str | None:
    match = _TIME_RE.search(raw)
    if not match:
        return None
    hour, minute = match.groups()
    return f"{int(hour):02d}:{minute}"


def _normalize_focus(raw: str) -> str:
    value = raw.strip().lower()
    if not value:
        return "general"
    return _FOCUS_MAP.get(value, value)

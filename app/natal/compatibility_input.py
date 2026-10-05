"""Validate pair questionnaires without trusting client-supplied coordinates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.natal.city_catalog import find_city_by_id, search_cities, search_countries
from app.natal.compatibility import (
    CompatibilityPair,
    CompatibilityQueryError,
    parse_compatibility_birth_date,
    parse_compatibility_start_payload,
)
from app.natal.geocoding import GeocodingError, resolve_local_birth_datetime
from app.natal.models import BirthInput, TimePrecision


@dataclass(frozen=True, slots=True)
class PairInput:
    pair: CompatibilityPair
    first: BirthInput
    second: BirthInput


class PairInputError(ValueError):
    """A fixed translation key, never a copy of private or untrusted input."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def parse_pair_input(payload: Any) -> PairInput:
    if not isinstance(payload, dict):
        raise PairInputError("form_error")
    try:
        pair, tarot = parse_compatibility_start_payload(_string(payload, "pair"))
    except CompatibilityQueryError as exc:
        raise PairInputError("form_error") from exc
    if tarot:
        raise PairInputError("form_error")
    return PairInput(
        pair, _partner_input(payload.get("first"), pair.language), _partner_input(payload.get("second"), pair.language)
    )


def _partner_input(payload: Any, lang: str) -> BirthInput:
    if not isinstance(payload, dict):
        raise PairInputError("form_error")
    raw_date = _string(payload, "birth_date")
    try:
        birth_date = parse_compatibility_birth_date(raw_date)
        if birth_date.isoformat() != raw_date:
            raise ValueError
    except ValueError as exc:
        raise PairInputError("date_error") from exc
    try:
        precision = TimePrecision(_string(payload, "time_precision") or "unknown")
    except ValueError as exc:
        raise PairInputError("time_error") from exc
    time_fields: dict[str, str] = {}
    if precision in {TimePrecision.EXACT, TimePrecision.APPROXIMATE}:
        time_fields["birth_time"] = _time(payload, "birth_time")
    elif precision == TimePrecision.RANGE:
        start, end = _time(payload, "birth_time_range_start"), _time(payload, "birth_time_range_end")
        if end <= start:
            raise PairInputError("time_error")
        time_fields = {"birth_time_range_start": start, "birth_time_range_end": end}
    country_code = _string(payload, "country_code").upper()
    city_id, city_query = _string(payload, "city_geoname_id"), _string(payload, "birth_place")
    place_fields: dict[str, Any] = {}
    if country_code or city_id or city_query:
        countries = search_countries(country_code, limit=1)
        if not country_code or not countries or countries[0].code != country_code:
            raise PairInputError("country_error")
        city = find_city_by_id(city_id) if city_id else None
        if not city_id and city_query:
            matches = search_cities(city_query, limit=1, country_code=country_code)
            city = matches[0] if matches else None
        if city is None or city.country_code != country_code:
            raise PairInputError("place_error")
        place_fields = {
            "birth_place": city.display_name,
            "birth_place_country_code": city.country_code,
            "birth_place_geoname_id": city.geoname_id,
            "birth_place_latitude": city.latitude,
            "birth_place_longitude": city.longitude,
            "birth_place_timezone": city.timezone,
            "birth_place_display_name": city.display_name,
        }
    elif precision != TimePrecision.UNKNOWN:
        raise PairInputError("place_error")
    birth = BirthInput(
        birth_date=birth_date.isoformat(),
        time_precision=precision,
        language=lang,
        focus="relationships",
        birth_time=time_fields.get("birth_time"),
        birth_time_range_start=time_fields.get("birth_time_range_start"),
        birth_time_range_end=time_fields.get("birth_time_range_end"),
        **place_fields,
    )
    if birth.birth_place_timezone:
        try:
            resolve_local_birth_datetime(birth, birth.birth_place_timezone)
        except GeocodingError as exc:
            raise PairInputError("local_time_error") from exc
    return birth


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > 200:
        raise PairInputError("form_error")
    return value.strip()


def _time(payload: dict[str, Any], key: str) -> str:
    value = _string(payload, key)
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise PairInputError("time_error")
    return value

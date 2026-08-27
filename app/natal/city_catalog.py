from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from math import cos, radians
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import geonamescache

# ⚡ Perf: json_compat wraps orjson for 2-6× faster JSON decode than stdlib.
from app.utils.json_compat import json

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "AM": ("Армения", "Armenia"),
    "AT": ("Австрия", "Austria"),
    "AZ": ("Азербайджан", "Azerbaijan"),
    "UA": ("Украина", "Україна", "Ukraine"),
    "RU": ("Россия", "РФ", "Russia"),
    "BY": ("Беларусь", "Белоруссия", "Belarus"),
    "KZ": ("Казахстан", "Kazakhstan"),
    "CA": ("Канада", "Canada"),
    "US": ("США", "Соединенные Штаты", "United States", "USA"),
    "GB": ("Великобритания", "Британия", "United Kingdom", "UK"),
    "DE": ("Германия", "Germany"),
    "CZ": ("Чехия", "Czechia", "Czech Republic"),
    "ES": ("Испания", "Spain"),
    "FR": ("Франция", "France"),
    "GE": ("Грузия", "Georgia"),
    "GR": ("Греция", "Greece"),
    "IL": ("Израиль", "Israel"),
    "IT": ("Италия", "Italy"),
    "LV": ("Латвия", "Latvia"),
    "LT": ("Литва", "Lithuania"),
    "MD": ("Молдова", "Молдавия", "Moldova"),
    "NL": ("Нидерланды", "Голландия", "Netherlands", "The Netherlands"),
    "PL": ("Польша", "Poland"),
    "RS": ("Сербия", "Serbia"),
    "TR": ("Турция", "Turkey"),
}

_ADMIN1_NAMES: dict[str, dict[str, str]] = {
    "CA": {
        "01": "Alberta",
        "02": "British Columbia",
        "03": "Manitoba",
        "04": "New Brunswick",
        "05": "Newfoundland and Labrador",
        "07": "Nova Scotia",
        "08": "Ontario",
        "09": "Prince Edward Island",
        "10": "Quebec",
        "11": "Saskatchewan",
        "12": "Yukon",
        "13": "Northwest Territories",
        "14": "Nunavut",
    },
    "UA": {
        "01": "Cherkasy Oblast",
        "02": "Chernihiv Oblast",
        "03": "Chernivtsi Oblast",
        "04": "Dnipropetrovsk Oblast",
        "05": "Donetsk Oblast",
        "06": "Ivano-Frankivsk Oblast",
        "07": "Kharkiv Oblast",
        "08": "Kherson Oblast",
        "09": "Khmelnytskyi Oblast",
        "10": "Kirovohrad Oblast",
        "11": "Crimea",
        "12": "Kyiv City",
        "13": "Kyiv Oblast",
        "14": "Luhansk Oblast",
        "15": "Lviv Oblast",
        "16": "Mykolaiv Oblast",
        "17": "Odesa Oblast",
        "18": "Poltava Oblast",
        "19": "Rivne Oblast",
        "20": "Sevastopol City",
        "21": "Sumy Oblast",
        "22": "Ternopil Oblast",
        "23": "Vinnytsia Oblast",
        "24": "Volyn Oblast",
        "25": "Zakarpattia Oblast",
        "26": "Zaporizhzhia Oblast",
        "27": "Zhytomyr Oblast",
    },
    "US": {
        "AL": "Alabama",
        "AK": "Alaska",
        "AZ": "Arizona",
        "AR": "Arkansas",
        "CA": "California",
        "CO": "Colorado",
        "CT": "Connecticut",
        "DE": "Delaware",
        "FL": "Florida",
        "GA": "Georgia",
        "HI": "Hawaii",
        "ID": "Idaho",
        "IL": "Illinois",
        "IN": "Indiana",
        "IA": "Iowa",
        "KS": "Kansas",
        "KY": "Kentucky",
        "LA": "Louisiana",
        "ME": "Maine",
        "MD": "Maryland",
        "MA": "Massachusetts",
        "MI": "Michigan",
        "MN": "Minnesota",
        "MS": "Mississippi",
        "MO": "Missouri",
        "MT": "Montana",
        "NE": "Nebraska",
        "NV": "Nevada",
        "NH": "New Hampshire",
        "NJ": "New Jersey",
        "NM": "New Mexico",
        "NY": "New York",
        "NC": "North Carolina",
        "ND": "North Dakota",
        "OH": "Ohio",
        "OK": "Oklahoma",
        "OR": "Oregon",
        "PA": "Pennsylvania",
        "RI": "Rhode Island",
        "SC": "South Carolina",
        "SD": "South Dakota",
        "TN": "Tennessee",
        "TX": "Texas",
        "UT": "Utah",
        "VT": "Vermont",
        "VA": "Virginia",
        "WA": "Washington",
        "WV": "West Virginia",
        "WI": "Wisconsin",
        "WY": "Wyoming",
        "DC": "District of Columbia",
    },
}


@dataclass(frozen=True)
class CountryRecord:
    code: str
    name: str
    population: int
    alternatenames: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.code})"


@dataclass(frozen=True)
class CityRecord:
    geoname_id: str
    name: str
    country_code: str
    admin1_code: str
    latitude: float
    longitude: float
    timezone: str
    population: int
    admin1_name: str | None = None
    alternatenames: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        country = _country_name(self.country_code)
        parts = [self.name]
        if self.admin1_name:
            parts.append(self.admin1_name)
        parts.append(country if country else self.country_code)
        return ", ".join(part for part in parts if part)


class CityCatalog:
    def __init__(self, cities: Iterable[CityRecord], countries: Iterable[CountryRecord] = ()) -> None:
        self._cities = list(cities)
        self._countries = list(countries)
        self._by_id = {city.geoname_id: city for city in self._cities}
        self._search_rows = [(city, _city_search_names(city)) for city in self._cities]
        self._city_prefix_index = _build_prefix_index(self._search_rows)
        self._country_search_rows = [
            (
                country,
                {
                    _normalize_token(country.name),
                    _normalize_token(country.code),
                    *(_normalize_token(name) for name in country.alternatenames if name),
                },
            )
            for country in self._countries
        ]

    def search(self, query: str, limit: int = 8, country_code: str | None = None) -> list[CityRecord]:
        normalized_query = _normalize_token(query)
        if not normalized_query:
            return []
        normalized_country = country_code.upper() if country_code else None
        scored: list[tuple[int, int, str, str, CityRecord]] = []
        seen: set[str] = set()
        for city, names in self.city_candidate_rows(normalized_query, country_code=normalized_country):
            seen.add(city.geoname_id)
            _score_city_row(scored, normalized_query, city, names)
        if len(scored) < max(1, limit):
            for city, names in self._search_rows:
                if city.geoname_id in seen:
                    continue
                if normalized_country and city.country_code != normalized_country:
                    continue
                _score_city_row(scored, normalized_query, city, names)
        scored.sort()
        return [city for _, _, _, _, city in scored[: max(1, limit)]]

    def city_candidate_rows(
        self,
        query: str,
        *,
        country_code: str | None = None,
    ) -> list[tuple[CityRecord, set[str]]]:
        normalized_query = _normalize_token(query)
        if not normalized_query:
            return []
        rows = self._city_prefix_index.get(normalized_query[0], [])
        normalized_country = country_code.upper() if country_code else None
        if normalized_country is None:
            return rows
        return [(city, names) for city, names in rows if city.country_code == normalized_country]

    def search_countries(self, query: str, limit: int = 8) -> list[CountryRecord]:
        normalized_query = _normalize_token(query)
        if not normalized_query:
            return []
        scored: list[tuple[int, int, str, str, CountryRecord]] = []
        for country, names in self._country_search_rows:
            best_score = _match_score(normalized_query, names)
            if best_score is None:
                continue
            scored.append((best_score, -country.population, country.name, country.code, country))
        scored.sort()
        return [country for _, _, _, _, country in scored[: max(1, limit)]]

    def find_by_id(self, geoname_id: str) -> CityRecord | None:
        return self._by_id.get(str(geoname_id))

    def nearest_timezone(self, latitude: float, longitude: float) -> str | None:
        nearest = min(
            self._cities,
            key=lambda city: _distance_key(latitude, longitude, city.latitude, city.longitude),
            default=None,
        )
        if nearest is None:
            return None
        return nearest.timezone


def search_cities(query: str, limit: int = 8, country_code: str | None = None) -> list[CityRecord]:
    return _catalog().search(query, limit, country_code=country_code)


def search_countries(query: str, limit: int = 8) -> list[CountryRecord]:
    return _catalog().search_countries(query, limit)


def find_city_by_id(geoname_id: str) -> CityRecord | None:
    return _catalog().find_by_id(geoname_id)


def nearest_city_timezone(latitude: float, longitude: float) -> str | None:
    return _catalog().nearest_timezone(latitude, longitude)


def warm_city_catalog() -> int:
    return len(_catalog()._cities)


def load_city_overrides(path: str | Path | None) -> list[CityRecord]:
    if path is None:
        return []
    if isinstance(path, str) and path.strip() in {"", "."}:
        return []
    override_path = Path(path)
    if override_path == Path("."):
        return []
    if not override_path.exists():
        raise ValueError(f"Natal city override file does not exist: {override_path}")
    raw = json.loads(override_path.read_text(encoding="utf-8"))
    raw_cities = raw.get("cities") if isinstance(raw, dict) else raw
    if not isinstance(raw_cities, list):
        raise ValueError("Natal city override file must contain a 'cities' list.")
    return [_city_record_from_override(item) for item in raw_cities]


@lru_cache(maxsize=1)
def _catalog() -> CityCatalog:
    gc = geonamescache.GeonamesCache()
    cities = gc.get_cities()
    countries = gc.get_countries()
    records = [
        CityRecord(
            geoname_id=str(raw["geonameid"]),
            name=str(raw["name"]),
            country_code=str(raw.get("countrycode") or ""),
            admin1_code=str(raw.get("admin1code") or ""),
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            timezone=str(raw["timezone"]),
            population=int(raw.get("population") or 0),
            admin1_name=_admin1_name(str(raw.get("countrycode") or ""), str(raw.get("admin1code") or "")),
            alternatenames=tuple(str(name) for name in raw.get("alternatenames") or ()),
        )
        for raw in cities.values()
        if raw.get("timezone") and raw.get("latitude") is not None and raw.get("longitude") is not None
    ]
    records.extend(load_city_overrides(os.getenv("NATAL_CITY_OVERRIDES_PATH")))
    country_populations: dict[str, int] = {}
    for city in records:
        country_populations[city.country_code] = country_populations.get(city.country_code, 0) + city.population
    country_records = [
        CountryRecord(
            code=str(code),
            name=str(raw.get("name") or code),
            population=country_populations.get(str(code), 0),
            alternatenames=_COUNTRY_ALIASES.get(str(code), ()),
        )
        for code, raw in countries.items()
    ]
    return CityCatalog(records, country_records)


@lru_cache(maxsize=1)
def _countries() -> dict[str, str]:
    gc = geonamescache.GeonamesCache()
    return {code: str(data.get("name") or code) for code, data in gc.get_countries().items()}


def _country_name(country_code: str) -> str:
    return _countries().get(country_code, country_code)


def _admin1_name(country_code: str, admin1_code: str) -> str | None:
    if not country_code or not admin1_code:
        return None
    return _ADMIN1_NAMES.get(country_code, {}).get(admin1_code)


def _score_city_row(
    scored: list[tuple[int, int, str, str, CityRecord]],
    query: str,
    city: CityRecord,
    names: set[str],
) -> None:
    best_score = _match_score(query, names)
    if best_score is None:
        return
    primary_name = _normalize_token(city.name)
    if query == primary_name:
        best_score = -1
    if city.admin1_name and query == _normalize_token(f"{city.name}, {city.admin1_name}"):
        best_score = -2
    scored.append((best_score, -city.population, city.name, city.geoname_id, city))


def _city_search_names(city: CityRecord) -> set[str]:
    names = {_normalize_token(city.name), *(_normalize_token(name) for name in city.alternatenames if name)}
    if city.admin1_name:
        names.add(_normalize_token(f"{city.name}, {city.admin1_name}"))
        names.add(_normalize_token(f"{city.name}, {city.admin1_name}, {_country_name(city.country_code)}"))
    names.add(_normalize_token(city.display_name))
    return names


def _build_prefix_index(
    rows: list[tuple[CityRecord, set[str]]],
) -> dict[str, list[tuple[CityRecord, set[str]]]]:
    indexed: dict[str, list[tuple[CityRecord, set[str]]]] = {}
    for city, names in rows:
        keys = {token[0] for name in names for token in (name, *name.split()) if token}
        for key in keys:
            indexed.setdefault(key, []).append((city, names))
    return indexed


def _normalize_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(normalized.split())


def _match_score(query: str, names: set[str]) -> int | None:
    best: int | None = None
    for name in names:
        if name == query:
            score = 0
        elif name.startswith(query):
            score = 1
        elif query in name:
            score = 2
        else:
            continue
        if best is None or score < best:
            best = score
    return best


def _distance_key(origin_latitude: float, origin_longitude: float, latitude: float, longitude: float) -> float:
    mean_latitude = radians((origin_latitude + latitude) / 2)
    latitude_delta = origin_latitude - latitude
    longitude_delta = (origin_longitude - longitude) * cos(mean_latitude)
    return latitude_delta * latitude_delta + longitude_delta * longitude_delta


def _city_record_from_override(raw: Any) -> CityRecord:
    if not isinstance(raw, dict):
        raise ValueError("Natal city override entries must be objects.")
    geoname_id = _required_override_str(raw, "geoname_id")
    name = _required_override_str(raw, "name")
    country_code = _required_override_str(raw, "country_code").upper()
    latitude = _required_override_float(raw, "latitude", geoname_id=geoname_id)
    longitude = _required_override_float(raw, "longitude", geoname_id=geoname_id)
    timezone = _required_override_str(raw, "timezone")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError(f"Natal city override {geoname_id} has invalid latitude.")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(f"Natal city override {geoname_id} has invalid longitude.")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Natal city override {geoname_id} has invalid timezone.") from exc
    alternatenames = raw.get("alternatenames") or ()
    if not isinstance(alternatenames, list | tuple):
        raise ValueError(f"Natal city override {geoname_id} alternatenames must be a list.")
    return CityRecord(
        geoname_id=geoname_id,
        name=name,
        country_code=country_code,
        admin1_code=str(raw.get("admin1_code") or ""),
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        population=int(raw.get("population") or 0),
        admin1_name=str(raw.get("admin1_name") or "") or None,
        alternatenames=tuple(str(value) for value in alternatenames),
    )


def _required_override_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"Natal city override entry is missing {key}.")
    return str(value).strip()


def _required_override_float(raw: dict[str, Any], key: str, *, geoname_id: str) -> float:
    try:
        return float(raw[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Natal city override {geoname_id} has invalid {key}.") from exc

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

import geonamescache


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
    alternatenames: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        country = _country_name(self.country_code)
        return f"{self.name}, {country}" if country else f"{self.name}, {self.country_code}"


class CityCatalog:
    def __init__(self, cities: Iterable[CityRecord]) -> None:
        self._cities = list(cities)
        self._by_id = {city.geoname_id: city for city in self._cities}
        self._search_rows = [
            (city, {_normalize_token(city.name), *(_normalize_token(name) for name in city.alternatenames if name)})
            for city in self._cities
        ]

    def search(self, query: str, limit: int = 8) -> list[CityRecord]:
        normalized_query = _normalize_token(query)
        if not normalized_query:
            return []
        scored: list[tuple[int, int, str, str, CityRecord]] = []
        for city, names in self._search_rows:
            best_score = _match_score(normalized_query, names)
            if best_score is None:
                continue
            scored.append((best_score, -city.population, city.name, city.geoname_id, city))
        scored.sort()
        return [city for _, _, _, _, city in scored[: max(1, limit)]]

    def find_by_id(self, geoname_id: str) -> CityRecord | None:
        return self._by_id.get(str(geoname_id))


def search_cities(query: str, limit: int = 8) -> list[CityRecord]:
    return _catalog().search(query, limit)


def find_city_by_id(geoname_id: str) -> CityRecord | None:
    return _catalog().find_by_id(geoname_id)


def warm_city_catalog() -> int:
    return len(_catalog()._cities)


@lru_cache(maxsize=1)
def _catalog() -> CityCatalog:
    gc = geonamescache.GeonamesCache()
    cities = gc.get_cities()
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
            alternatenames=tuple(str(name) for name in raw.get("alternatenames") or ()),
        )
        for raw in cities.values()
        if raw.get("timezone") and raw.get("latitude") is not None and raw.get("longitude") is not None
    ]
    return CityCatalog(records)


@lru_cache(maxsize=1)
def _countries() -> dict[str, str]:
    gc = geonamescache.GeonamesCache()
    return {code: str(data.get("name") or code) for code, data in gc.get_countries().items()}


def _country_name(country_code: str) -> str:
    return _countries().get(country_code, country_code)


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

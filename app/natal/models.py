from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TimePrecision(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    RANGE = "range"
    UNKNOWN = "unknown"


class BirthInput(BaseModel):
    birth_date: str
    time_precision: TimePrecision
    birth_time: str | None = None
    birth_time_range_start: str | None = None
    birth_time_range_end: str | None = None
    birth_place: str
    birth_place_geoname_id: str | None = None
    birth_place_latitude: float | None = None
    birth_place_longitude: float | None = None
    birth_place_timezone: str | None = None
    birth_place_display_name: str | None = None
    language: str = "ru"
    focus: str = "general"


class ResolvedBirthData(BaseModel):
    birth_input: BirthInput
    latitude: float
    longitude: float
    timezone: str
    local_datetime: str
    utc_datetime: str
    display_place: str


class InputQuality(BaseModel):
    time_precision: TimePrecision
    houses_available: bool
    angles_available: bool
    calculation_engine: str = "ephem-local"
    reference_validated: bool = False
    moon_uncertainty: bool = False
    warnings: list[str] = Field(default_factory=list)


class PlanetPosition(BaseModel):
    key: str
    label: str
    longitude: float
    sign: str
    degree_in_sign: float
    house: int | None = None
    retrograde: bool = False


class Aspect(BaseModel):
    point_a: str
    point_b: str
    aspect: str
    orb: float
    applying: bool | None = None


class House(BaseModel):
    number: int
    cusp_longitude: float
    sign: str


class ChartData(BaseModel):
    input_quality: InputQuality
    planets: list[PlanetPosition]
    aspects: list[Aspect]
    houses: list[House] = Field(default_factory=list)
    angles: dict[str, float] = Field(default_factory=dict)


class ReportSection(BaseModel):
    id: str
    title: str
    body_markdown: str
    chart_refs: list[str] = Field(default_factory=list)


class NatalReport(BaseModel):
    report_id: str
    user_id: int
    chart: ChartData
    svg: str
    sections: list[ReportSection]
    hosted_url: str | None = None
    telegraph_url: str | None = None

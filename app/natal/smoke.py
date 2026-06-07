from __future__ import annotations

from dataclasses import dataclass

from app.natal.models import BirthInput, TimePrecision
from app.natal.service import create_natal_report


@dataclass(frozen=True)
class NatalSmokeResult:
    report_id: str
    hosted_url: str
    telegraph_url: str | None
    planet_count: int
    section_count: int


async def run_natal_smoke(webhook_url: str, user_id: int = 0, chat_id: int = 0) -> NatalSmokeResult:
    birth_input = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.EXACT,
        birth_time="06:30",
        birth_place="Odesa",
        birth_place_country_code="UA",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
        language="ru",
        focus="general",
    )
    report = await create_natal_report(
        birth_input=birth_input,
        user_id=user_id,
        chat_id=chat_id,
        webhook_url=webhook_url,
    )
    if not report.report_id:
        raise RuntimeError("Natal smoke failed: report_id is empty.")
    if not report.hosted_url:
        raise RuntimeError("Natal smoke failed: hosted_url is empty.")
    if not report.svg.lstrip().startswith("<svg"):
        raise RuntimeError("Natal smoke failed: SVG was not generated.")
    if not report.sections:
        raise RuntimeError("Natal smoke failed: interpretation sections are empty.")
    if not report.chart.planets:
        raise RuntimeError("Natal smoke failed: chart has no planets.")
    return NatalSmokeResult(
        report_id=report.report_id,
        hosted_url=report.hosted_url,
        telegraph_url=report.telegraph_url,
        planet_count=len(report.chart.planets),
        section_count=len(report.sections),
    )

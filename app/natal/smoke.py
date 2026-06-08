from __future__ import annotations

from dataclasses import dataclass

from app.natal.models import BirthInput, TimePrecision
from app.natal.report_builder import build_hosted_report_html
from app.natal.report_ids import is_valid_report_id
from app.natal.service import create_natal_report
from app.natal.storage import check_storage_ready, get_report


@dataclass(frozen=True)
class NatalSmokeResult:
    report_id: str
    hosted_url: str
    telegraph_url: str | None
    planet_count: int
    section_count: int
    hosted_html_contains_svg: bool
    hosted_html_contains_sections: bool


async def run_natal_smoke(webhook_url: str, user_id: int = 0, chat_id: int = 0) -> NatalSmokeResult:
    await check_storage_ready()
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
    if not is_valid_report_id(report.report_id):
        raise RuntimeError("Natal smoke failed: report_id format is incompatible with hosted route.")
    if not report.hosted_url:
        raise RuntimeError("Natal smoke failed: hosted_url is empty.")
    if not report.hosted_url.rstrip("/").endswith(f"/reports/natal/{report.report_id}"):
        raise RuntimeError("Natal smoke failed: hosted_url does not match report_id.")
    if not report.svg.lstrip().startswith("<svg"):
        raise RuntimeError("Natal smoke failed: SVG was not generated.")
    if not report.sections:
        raise RuntimeError("Natal smoke failed: interpretation sections are empty.")
    if not report.chart.planets:
        raise RuntimeError("Natal smoke failed: chart has no planets.")
    stored_report = await get_report(report.report_id)
    if stored_report is None:
        raise RuntimeError("Natal smoke failed: report was not readable from storage.")
    hosted_html = build_hosted_report_html(stored_report)
    hosted_html_contains_svg = "<svg" in hosted_html
    hosted_html_contains_sections = all(f'id="{section.id}"' in hosted_html for section in stored_report.sections)
    if not hosted_html_contains_svg:
        raise RuntimeError("Natal smoke failed: hosted HTML does not contain SVG.")
    if not hosted_html_contains_sections:
        raise RuntimeError("Natal smoke failed: hosted HTML does not contain all section ids.")
    return NatalSmokeResult(
        report_id=report.report_id,
        hosted_url=report.hosted_url,
        telegraph_url=report.telegraph_url,
        planet_count=len(report.chart.planets),
        section_count=len(report.sections),
        hosted_html_contains_svg=hosted_html_contains_svg,
        hosted_html_contains_sections=hosted_html_contains_sections,
    )

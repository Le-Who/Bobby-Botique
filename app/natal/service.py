from __future__ import annotations

import logging
import os
import secrets

from app.natal.calculator import calculate_chart
from app.natal.destiny_matrix import build_destiny_matrix_sections, calculate_destiny_matrix, render_destiny_matrix_svg
from app.natal.geocoding import resolve_birth_data
from app.natal.llm import generate_interpretation
from app.natal.models import BirthInput, ChartData, InputQuality, NatalReport, ReportType, TimePrecision
from app.natal.report_builder import build_telegraph_markdown
from app.natal.storage import save_report
from app.natal.svg_renderer import render_chart_svg
from app.utils.telegraph import create_telegraph_page_from_markdown

logger = logging.getLogger(__name__)

_TELEGRAPH_MARKDOWN_MAX_CHARS = 60_000


class NatalReportError(RuntimeError):
    pass


class NatalConfigurationError(NatalReportError):
    pass


async def create_natal_report(
    birth_input: BirthInput,
    user_id: int,
    chat_id: int,
    webhook_url: str,
) -> NatalReport:
    if not _natal_reports_enabled():
        raise NatalConfigurationError("Natal reports are disabled.")
    _validate_webhook_url(webhook_url)
    report_type = birth_input.report_type
    matrix = calculate_destiny_matrix(birth_input.birth_date) if _includes_destiny_matrix(report_type) else None
    if _requires_natal_chart(report_type):
        resolved = await resolve_birth_data(birth_input, geocoder_provider=_natal_geocoder_provider())
        chart = await calculate_chart(resolved)
        chart.destiny_matrix = matrix
        svg = render_chart_svg(chart)
        sections = await generate_interpretation(
            chart,
            user_id=user_id,
            chat_id=chat_id,
            language=birth_input.language,
            focus=birth_input.focus,
        )
    else:
        chart = _empty_matrix_chart(matrix)
        svg = render_destiny_matrix_svg(matrix) if matrix is not None else ""
        sections = []
    if matrix is not None:
        sections.extend(build_destiny_matrix_sections(matrix))
    report_id = secrets.token_urlsafe(16)
    hosted_url = f"{webhook_url.rstrip('/')}/reports/natal/{report_id}"
    report = NatalReport(
        report_id=report_id,
        user_id=user_id,
        chart=chart,
        svg=svg,
        sections=sections,
        hosted_url=hosted_url,
    )
    await save_report(report)
    telegraph_url = await _try_publish_telegraph(report)
    if _is_safe_telegraph_url(telegraph_url):
        report.telegraph_url = telegraph_url
        await save_report(report)
    return report


def _requires_natal_chart(report_type: ReportType) -> bool:
    return report_type in {ReportType.NATAL, ReportType.COMBINED}


def _includes_destiny_matrix(report_type: ReportType) -> bool:
    return report_type in {ReportType.DESTINY_MATRIX, ReportType.COMBINED}


def _empty_matrix_chart(matrix) -> ChartData:
    return ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN,
            houses_available=False,
            angles_available=False,
            calculation_engine="destiny-matrix-local",
            warnings=[],
        ),
        planets=[],
        aspects=[],
        destiny_matrix=matrix,
    )


def _validate_webhook_url(webhook_url: str) -> None:
    if not webhook_url:
        raise NatalConfigurationError("WEBHOOK_URL is required for hosted natal reports.")
    is_production = bool(os.getenv("DATABASE_URL"))
    if is_production and not webhook_url.startswith("https://"):
        raise NatalConfigurationError("HTTPS WEBHOOK_URL is required in production.")


def _natal_reports_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "NATAL_REPORTS_ENABLED", False))


def _natal_geocoder_provider() -> str:
    from app.config import settings

    return str(getattr(settings, "NATAL_GEOCODER_PROVIDER", "local") or "local").strip().lower()


async def _try_publish_telegraph(report: NatalReport) -> str | None:
    try:
        markdown = build_telegraph_markdown(report)
        if len(markdown) > _TELEGRAPH_MARKDOWN_MAX_CHARS:
            logger.info(
                "Skipping natal Telegraph mirror: markdown is too large (%d chars)",
                len(markdown),
            )
            return None
        return await create_telegraph_page_from_markdown("Натальная карта", markdown)
    except Exception as exc:
        logger.warning("Natal Telegraph mirror creation failed: %s", exc)
        return None


def _is_safe_telegraph_url(value: str | None) -> bool:
    return bool(value and value.strip().lower().startswith("https://"))

from __future__ import annotations

import os
import secrets

from app.natal.calculator import calculate_chart
from app.natal.geocoding import resolve_birth_data
from app.natal.llm import generate_interpretation
from app.natal.models import BirthInput, NatalReport
from app.natal.report_builder import build_telegraph_markdown
from app.natal.storage import save_report
from app.natal.svg_renderer import render_chart_svg
from app.utils.telegraph import create_telegraph_page_from_markdown


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
    resolved = await resolve_birth_data(birth_input, geocoder_provider=_natal_geocoder_provider())
    chart = await calculate_chart(resolved)
    svg = render_chart_svg(chart)
    sections = await generate_interpretation(
        chart,
        user_id=user_id,
        chat_id=chat_id,
        language=birth_input.language,
        focus=birth_input.focus,
    )
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
    markdown = build_telegraph_markdown(report)
    return await create_telegraph_page_from_markdown("Натальная карта", markdown)


def _is_safe_telegraph_url(value: str | None) -> bool:
    return bool(value and value.strip().lower().startswith("https://"))

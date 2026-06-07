from __future__ import annotations

from app.database import db_query
from app.natal.models import ChartData, NatalReport, ReportSection
from app.utils.json_compat import json


async def save_report(report: NatalReport) -> None:
    await db_query(
        """
        INSERT INTO natal_reports (
            report_id, user_id, chart_json, svg, sections_json, hosted_url, telegraph_url
        )
        VALUES ($1, $2, $3::jsonb, $4, $5::jsonb, $6, $7)
        ON CONFLICT (report_id) DO UPDATE SET
            chart_json = EXCLUDED.chart_json,
            svg = EXCLUDED.svg,
            sections_json = EXCLUDED.sections_json,
            hosted_url = EXCLUDED.hosted_url,
            telegraph_url = EXCLUDED.telegraph_url
        """,
        (
            report.report_id,
            report.user_id,
            report.chart.model_dump(mode="json"),
            report.svg,
            [section.model_dump(mode="json") for section in report.sections],
            report.hosted_url,
            report.telegraph_url,
        ),
    )


async def get_report(report_id: str) -> NatalReport | None:
    rows = await db_query(
        """
        SELECT report_id, user_id, chart_json, svg, sections_json, hosted_url, telegraph_url
        FROM natal_reports
        WHERE report_id = $1 AND deleted_at IS NULL
        """,
        (report_id,),
    )
    if not rows:
        return None
    row = rows[0]
    chart_json = _ensure_json(row["chart_json"])
    sections_json = _ensure_json(row["sections_json"])
    return NatalReport(
        report_id=row["report_id"],
        user_id=int(row["user_id"]),
        chart=ChartData.model_validate(chart_json),
        svg=row["svg"],
        sections=[ReportSection.model_validate(section) for section in sections_json],
        hosted_url=row.get("hosted_url"),
        telegraph_url=row.get("telegraph_url"),
    )


async def mark_report_deleted(report_id: str, user_id: int) -> bool:
    rows = await db_query(
        """
        UPDATE natal_reports
        SET deleted_at = NOW()
        WHERE report_id = $1 AND user_id = $2 AND deleted_at IS NULL
        RETURNING report_id
        """,
        (report_id, user_id),
    )
    return bool(rows)


def _ensure_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value

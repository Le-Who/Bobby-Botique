from __future__ import annotations

from app.database import db_query
from app.natal.models import ChartData, NatalReport, ReportSection
from app.utils.json_compat import json


class NatalStorageError(RuntimeError):
    pass


_REQUIRED_COLUMNS = {
    "report_id": ("text", "NO"),
    "user_id": ("bigint", "NO"),
    "chart_json": ("jsonb", "NO"),
    "svg": ("text", "NO"),
    "sections_json": ("jsonb", "NO"),
    "hosted_url": ("text", "YES"),
    "telegraph_url": ("text", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "deleted_at": ("timestamp with time zone", "YES"),
}
_REQUIRED_INDEXES = {"idx_natal_reports_user_created"}
_REQUIRED_INDEX_DEFINITIONS = {
    "idx_natal_reports_user_created": (
        "user_id",
        "created_at desc",
        "deleted_at is null",
    )
}


async def check_storage_ready() -> None:
    rows = await db_query(
        "SELECT to_regclass('public.natal_reports') AS table_name",
        (),
    )
    table_name = rows[0].get("table_name") if rows else None
    if table_name != "natal_reports":
        raise NatalStorageError(
            "natal_reports table is missing. Apply scripts/migrations/050_add_natal_reports.sql before enabling natal reports."
        )
    column_rows = await db_query(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'natal_reports'
        """,
        (),
    )
    columns = {str(row["column_name"]): (str(row["data_type"]), str(row["is_nullable"])) for row in column_rows}
    missing_columns = sorted(set(_REQUIRED_COLUMNS) - set(columns))
    if missing_columns:
        raise NatalStorageError(
            "natal_reports table is missing required columns: "
            + ", ".join(missing_columns)
            + ". Re-apply scripts/migrations/050_add_natal_reports.sql."
        )
    mismatched_columns = [
        f"{name} expected {expected_type} nullable={expected_nullable}, got {actual_type} nullable={actual_nullable}"
        for name, (expected_type, expected_nullable) in _REQUIRED_COLUMNS.items()
        for actual_type, actual_nullable in [columns[name]]
        if (actual_type, actual_nullable) != (expected_type, expected_nullable)
    ]
    if mismatched_columns:
        raise NatalStorageError(
            "natal_reports table has incompatible columns: "
            + "; ".join(mismatched_columns)
            + ". Re-apply scripts/migrations/050_add_natal_reports.sql."
        )
    index_rows = await db_query(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'natal_reports'
        """,
        (),
    )
    indexes = {str(row["indexname"]) for row in index_rows}
    missing_indexes = sorted(_REQUIRED_INDEXES - indexes)
    if missing_indexes:
        raise NatalStorageError(
            "natal_reports table is missing required indexes: "
            + ", ".join(missing_indexes)
            + ". Re-apply scripts/migrations/050_add_natal_reports.sql."
        )
    incompatible_indexes = []
    index_definitions = {str(row["indexname"]): str(row.get("indexdef", "")) for row in index_rows}
    for index_name, required_parts in _REQUIRED_INDEX_DEFINITIONS.items():
        normalized_definition = _normalize_sql(index_definitions.get(index_name, ""))
        if not all(required_part in normalized_definition for required_part in required_parts):
            incompatible_indexes.append(index_name)
    if incompatible_indexes:
        raise NatalStorageError(
            "natal_reports table has incompatible indexes: "
            + ", ".join(sorted(incompatible_indexes))
            + ". Re-apply scripts/migrations/050_add_natal_reports.sql."
        )


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
            json.dumps(report.chart.model_dump(mode="json")),
            report.svg,
            json.dumps([section.model_dump(mode="json") for section in report.sections]),
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
        hosted_url=_row_get(row, "hosted_url"),
        telegraph_url=_row_get(row, "telegraph_url"),
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


async def purge_expired_reports(ttl_days: int) -> int:
    if ttl_days <= 0:
        raise NatalStorageError("NATAL_REPORT_TTL_DAYS must be positive.")
    rows = await db_query(
        """
        UPDATE natal_reports
        SET deleted_at = NOW()
        WHERE deleted_at IS NULL
          AND created_at < NOW() - ($1::int * INTERVAL '1 day')
        RETURNING report_id
        """,
        (ttl_days,),
    )
    return len(rows)


def _ensure_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_get(row, key: str):
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except KeyError:
        return None


def _normalize_sql(value: str) -> str:
    return " ".join(value.lower().replace("(", " ").replace(")", " ").replace(",", " ").split())

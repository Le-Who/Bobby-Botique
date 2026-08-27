from pathlib import Path

import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.natal.storage import (
    NatalStorageError,
    check_storage_ready,
    get_report,
    mark_report_deleted,
    purge_expired_reports,
    save_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_report() -> NatalReport:
    return NatalReport(
        report_id="report-1",
        user_id=123,
        chart=ChartData(
            input_quality=InputQuality(
                time_precision=TimePrecision.UNKNOWN,
                houses_available=False,
                angles_available=False,
            ),
            planets=[
                PlanetPosition(
                    key="sun",
                    label="Солнце",
                    longitude=325,
                    sign="Водолей",
                    degree_in_sign=25,
                )
            ],
            aspects=[],
        ),
        svg="<svg></svg>",
        sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="body")],
        hosted_url="https://example.com/reports/natal/report-1",
    )


def storage_column_rows() -> list[dict[str, str]]:
    return [
        {"column_name": "report_id", "data_type": "text", "is_nullable": "NO"},
        {"column_name": "user_id", "data_type": "bigint", "is_nullable": "NO"},
        {"column_name": "chart_json", "data_type": "jsonb", "is_nullable": "NO"},
        {"column_name": "svg", "data_type": "text", "is_nullable": "NO"},
        {"column_name": "sections_json", "data_type": "jsonb", "is_nullable": "NO"},
        {"column_name": "hosted_url", "data_type": "text", "is_nullable": "YES"},
        {"column_name": "telegraph_url", "data_type": "text", "is_nullable": "YES"},
        {"column_name": "created_at", "data_type": "timestamp with time zone", "is_nullable": "NO"},
        {"column_name": "deleted_at", "data_type": "timestamp with time zone", "is_nullable": "YES"},
    ]


def storage_index_rows() -> list[dict[str, str]]:
    return [
        {
            "indexname": "idx_natal_reports_user_created",
            "indexdef": (
                "CREATE INDEX idx_natal_reports_user_created ON public.natal_reports "
                "USING btree (user_id, created_at DESC) WHERE (deleted_at IS NULL)"
            ),
        }
    ]


def test_natal_reports_migration_matches_storage_readiness_contract():
    sql = (PROJECT_ROOT / "scripts" / "migrations" / "050_add_natal_reports.sql").read_text(encoding="utf-8").lower()

    assert "create table if not exists natal_reports" in sql
    for column in (
        "report_id text primary key",
        "user_id bigint not null",
        "chart_json jsonb not null",
        "svg text not null",
        "sections_json jsonb not null",
        "hosted_url text",
        "telegraph_url text",
        "created_at timestamptz not null default now()",
        "deleted_at timestamptz",
    ):
        assert column in " ".join(sql.split())
    assert "create index if not exists idx_natal_reports_user_created" in sql
    assert "on natal_reports(user_id, created_at desc)" in " ".join(sql.split())
    assert "where deleted_at is null" in " ".join(sql.split())


class RowWithoutGet:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def __getitem__(self, key: str):
        return self._values[key]


@pytest.mark.asyncio
async def test_save_report_stores_payload(monkeypatch):
    calls = []

    async def fake_db_query(query, params=(), retries=3, conn=None):
        calls.append((query, params))
        return []

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    await save_report(make_report())

    query, params = calls[0]
    assert "INSERT INTO natal_reports" in query
    assert params[0] == "report-1"
    assert params[1] == 123
    assert isinstance(params[2], str)
    assert params[3] == "<svg></svg>"
    assert isinstance(params[4], str)


@pytest.mark.asyncio
async def test_get_report_returns_none_when_missing(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        return []

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    assert await get_report("missing") is None


@pytest.mark.asyncio
async def test_get_report_rehydrates_report_from_row_without_get(monkeypatch):
    saved = make_report()
    row = RowWithoutGet(
        {
            "report_id": saved.report_id,
            "user_id": saved.user_id,
            "chart_json": saved.chart.model_dump(mode="json"),
            "svg": saved.svg,
            "sections_json": [section.model_dump(mode="json") for section in saved.sections],
            "hosted_url": saved.hosted_url,
            "telegraph_url": None,
        }
    )

    async def fake_db_query(query, params=(), retries=3, conn=None):
        assert "SELECT report_id" in query
        assert params == ("report-1",)
        return [row]

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    report = await get_report("report-1")

    assert report is not None
    assert report.report_id == "report-1"
    assert report.hosted_url == saved.hosted_url
    assert report.telegraph_url is None
    assert report.sections[0].id == "section-sun"


@pytest.mark.asyncio
async def test_delete_report_marks_deleted(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        assert "UPDATE natal_reports" in query
        assert params == ("report-1", 123)
        return [{"report_id": "report-1"}]

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    assert await mark_report_deleted("report-1", 123) is True


@pytest.mark.asyncio
async def test_check_storage_ready_raises_when_table_is_missing(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        assert "to_regclass" in query
        return [{"table_name": None}]

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    with pytest.raises(NatalStorageError, match="natal_reports"):
        await check_storage_ready()


@pytest.mark.asyncio
async def test_check_storage_ready_raises_when_required_columns_are_missing(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "to_regclass" in query:
            return [{"table_name": "natal_reports"}]
        if "information_schema.columns" in query:
            return [
                {"column_name": "report_id", "data_type": "text", "is_nullable": "NO"},
                {"column_name": "user_id", "data_type": "bigint", "is_nullable": "NO"},
            ]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    with pytest.raises(NatalStorageError, match="missing required columns"):
        await check_storage_ready()


@pytest.mark.asyncio
async def test_check_storage_ready_raises_when_required_index_is_missing(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "to_regclass" in query:
            return [{"table_name": "natal_reports"}]
        if "information_schema.columns" in query:
            return storage_column_rows()
        if "pg_indexes" in query:
            return []
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    with pytest.raises(NatalStorageError, match="idx_natal_reports_user_created"):
        await check_storage_ready()


@pytest.mark.asyncio
async def test_check_storage_ready_accepts_required_schema_and_index(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "to_regclass" in query:
            return [{"table_name": "natal_reports"}]
        if "information_schema.columns" in query:
            return storage_column_rows()
        if "pg_indexes" in query:
            return storage_index_rows()
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    await check_storage_ready()


@pytest.mark.asyncio
async def test_check_storage_ready_rejects_wrong_index_definition(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        if "to_regclass" in query:
            return [{"table_name": "natal_reports"}]
        if "information_schema.columns" in query:
            return storage_column_rows()
        if "pg_indexes" in query:
            return [
                {
                    "indexname": "idx_natal_reports_user_created",
                    "indexdef": (
                        "CREATE INDEX idx_natal_reports_user_created ON public.natal_reports USING btree (user_id)"
                    ),
                }
            ]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    with pytest.raises(NatalStorageError, match="incompatible indexes"):
        await check_storage_ready()


@pytest.mark.asyncio
async def test_purge_expired_reports_marks_old_reports_deleted(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        assert "UPDATE natal_reports" in query
        assert "created_at < NOW() - ($1::int * INTERVAL '1 day')" in query
        assert params == (365,)
        return [{"report_id": "old-1"}, {"report_id": "old-2"}]

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    assert await purge_expired_reports(ttl_days=365) == 2


@pytest.mark.asyncio
async def test_purge_expired_reports_rejects_non_positive_ttl():
    with pytest.raises(NatalStorageError, match="positive"):
        await purge_expired_reports(ttl_days=0)

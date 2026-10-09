"""product-04: actual JSONB/SVG roundtrip, deletion and maintenance TTL."""

import pytest
from quart import Quart, g

from app.natal import storage
from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.web_natal import natal_bp

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def report(report_id="contract-report-123456", user_id=82001):
    return NatalReport(
        report_id=report_id,
        user_id=user_id,
        chart=ChartData(
            input_quality=InputQuality(
                time_precision=TimePrecision.UNKNOWN, houses_available=False, angles_available=False
            ),
            planets=[PlanetPosition(key="sun", label="Солнце", longitude=325, sign="Водолей", degree_in_sign=25)],
            aspects=[],
        ),
        svg='<svg xmlns="http://www.w3.org/2000/svg"><title>Карта</title></svg>',
        sections=[ReportSection(id="sun", title="Солнце", body_markdown="Контрольный текст")],
        hosted_url="https://example.invalid/report",
        telegraph_url=None,
    )


def client():
    application = Quart(__name__)
    application.register_blueprint(natal_bp)

    @application.before_request
    async def nonce():
        g.csp_nonce = "controlled-nonce"

    return application.test_client()


async def test_save_hydrate_owner_delete_http404_and_resave_tombstone(db_conn):
    # Losing the owner predicate or clearing deleted_at on UPSERT would violate privacy.
    await db_conn.execute("INSERT INTO users(user_id) VALUES(82001)")
    original = report()
    await storage.save_report(original)
    await storage.check_storage_ready()
    hydrated = await storage.get_report(original.report_id)
    assert hydrated.model_dump(mode="json") == original.model_dump(mode="json")
    assert (await client().get("/reports/natal/" + original.report_id)).status_code == 200
    assert await storage.mark_report_deleted(original.report_id, 82002) is False
    assert await storage.get_report(original.report_id) is not None
    assert await storage.mark_report_deleted(original.report_id, original.user_id) is True
    assert await storage.mark_report_deleted(original.report_id, original.user_id) is False
    assert await storage.get_report(original.report_id) is None
    assert (await client().get("/reports/natal/" + original.report_id)).status_code == 404
    original.svg = "<svg><title>Replacement</title></svg>"
    await storage.save_report(original)
    assert await storage.get_report(original.report_id) is None
    assert await db_conn.fetchval(
        "SELECT deleted_at IS NOT NULL FROM natal_reports WHERE report_id=$1", original.report_id
    )
    assert (await client().get("/reports/natal/missing-report-123456")).status_code == 404


async def test_ttl_is_maintenance_enforced_and_preserves_new_report(db_conn):
    await db_conn.execute("INSERT INTO users(user_id) VALUES(82001)")
    expired = report("expired-report-123456")
    fresh = report("fresh-report-123456")
    await storage.save_report(expired)
    await storage.save_report(fresh)
    await db_conn.execute(
        "UPDATE natal_reports SET created_at=now()-interval '31 days' WHERE report_id=$1", expired.report_id
    )
    assert await storage.get_report(expired.report_id) is not None
    assert (await client().get("/reports/natal/" + expired.report_id)).status_code == 200
    assert await storage.purge_expired_reports(30) == 1
    assert await storage.get_report(expired.report_id) is None
    assert (await client().get("/reports/natal/" + expired.report_id)).status_code == 404
    assert (await storage.get_report(fresh.report_id)).svg == fresh.svg
    assert await storage.purge_expired_reports(30) == 0

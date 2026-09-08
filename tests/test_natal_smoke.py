import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.natal.smoke import run_natal_smoke

VALID_SMOKE_REPORT_ID = "smoke-report-id-123456"


@pytest.fixture(autouse=True)
def existing_smoke_user(monkeypatch):
    async def fake_db_query(query, params):
        assert query == "SELECT user_id FROM public.users WHERE user_id = $1"
        assert params == (1,)
        return [{"user_id": 1}]

    monkeypatch.setattr("app.natal.smoke.db_query", fake_db_query, raising=False)


@pytest.mark.asyncio
async def test_smoke_rejects_unconfigured_user_before_any_io(monkeypatch):
    async def unexpected_io(*args, **kwargs):
        pytest.fail("invalid identity must fail before database or generation")

    monkeypatch.setattr("app.natal.smoke.check_storage_ready", unexpected_io)
    with pytest.raises(ValueError, match="positive.*user"):
        await run_natal_smoke(webhook_url="https://bot.example.com")


@pytest.mark.asyncio
async def test_smoke_rejects_missing_user_before_generation(monkeypatch):
    async def ready():
        pass

    async def no_user(query, params):
        assert params == (123,)
        return []

    async def unexpected_generation(*args, **kwargs):
        pytest.fail("missing user must fail before paid generation")

    monkeypatch.setattr("app.natal.smoke.check_storage_ready", ready)
    monkeypatch.setattr("app.natal.smoke.db_query", no_user, raising=False)
    monkeypatch.setattr("app.natal.smoke.create_natal_report", unexpected_generation)
    with pytest.raises(ValueError, match="registered"):
        await run_natal_smoke(webhook_url="https://bot.example.com", user_id=123)


@pytest.mark.asyncio
async def test_run_natal_smoke_creates_sample_report(monkeypatch):
    captured = {}
    saved_report = None
    storage_checked = False

    async def fake_check_storage_ready():
        nonlocal storage_checked
        storage_checked = True

    async def fake_create_natal_report(birth_input, user_id, chat_id, webhook_url):
        assert storage_checked is True
        captured["birth_input"] = birth_input
        captured["user_id"] = user_id
        captured["chat_id"] = chat_id
        captured["webhook_url"] = webhook_url
        nonlocal saved_report
        saved_report = NatalReport(
            report_id=VALID_SMOKE_REPORT_ID,
            user_id=user_id,
            chart=ChartData(
                input_quality=InputQuality(
                    time_precision=TimePrecision.EXACT,
                    houses_available=True,
                    angles_available=True,
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
            hosted_url=f"{webhook_url}/reports/natal/{VALID_SMOKE_REPORT_ID}",
        )
        return saved_report

    async def fake_get_report(report_id):
        assert report_id == VALID_SMOKE_REPORT_ID
        return saved_report

    monkeypatch.setattr("app.natal.smoke.check_storage_ready", fake_check_storage_ready)
    monkeypatch.setattr("app.natal.smoke.create_natal_report", fake_create_natal_report)
    monkeypatch.setattr("app.natal.smoke.get_report", fake_get_report)

    result = await run_natal_smoke(webhook_url="https://bot.example.com", user_id=1, chat_id=2)

    assert result.report_id == VALID_SMOKE_REPORT_ID
    assert result.hosted_url == f"https://bot.example.com/reports/natal/{VALID_SMOKE_REPORT_ID}"
    assert captured["birth_input"].birth_place_display_name == "Odesa, Ukraine"
    assert captured["birth_input"].birth_place_timezone == "Europe/Kyiv"
    assert captured["user_id"] == 1
    assert captured["chat_id"] == 2
    assert result.hosted_html_contains_svg is True
    assert result.hosted_html_contains_sections is True


@pytest.mark.asyncio
async def test_run_natal_smoke_rejects_invalid_report_id(monkeypatch):
    async def fake_check_storage_ready():
        return None

    async def fake_create_natal_report(birth_input, user_id, chat_id, webhook_url):
        return NatalReport(
            report_id="bad.id",
            user_id=user_id,
            chart=ChartData(
                input_quality=InputQuality(
                    time_precision=TimePrecision.EXACT,
                    houses_available=True,
                    angles_available=True,
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
            hosted_url=f"{webhook_url}/reports/natal/bad.id",
        )

    async def fake_get_report(report_id):
        raise AssertionError("invalid smoke report id must not reach storage retrieval")

    monkeypatch.setattr("app.natal.smoke.check_storage_ready", fake_check_storage_ready)
    monkeypatch.setattr("app.natal.smoke.create_natal_report", fake_create_natal_report)
    monkeypatch.setattr("app.natal.smoke.get_report", fake_get_report)

    with pytest.raises(RuntimeError, match="report_id format"):
        await run_natal_smoke(webhook_url="https://bot.example.com", user_id=1, chat_id=2)

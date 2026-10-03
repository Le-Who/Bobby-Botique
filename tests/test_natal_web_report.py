import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision


def _run_browser_regression(payload: dict, scenario: str) -> None:
    browser_root = Path(__file__).resolve().parent / "browser"
    required = os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1"
    node = shutil.which("node")
    if node is None:
        if required:
            pytest.fail("Required browser regression cannot run: Node.js is missing")
        pytest.skip("Node.js is needed for the optional browser regression")
    probe = subprocess.run(
        [node, "-e", "require.resolve('playwright')"],
        cwd=browser_root,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    if probe.returncode:
        if required:
            pytest.fail("Required browser regression cannot run: install tests/browser dependencies with npm ci")
        pytest.skip("Playwright is needed for the optional browser regression")
    payload["navigation_script"] = (Path(__file__).resolve().parents[1] / "app/static/js/natal-report.js").read_text(
        encoding="utf-8"
    )
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 844 }, reducedMotion: 'reduce' });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let submitted;
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'telegram.org') return route.fulfill({ body: '', contentType: 'application/javascript' });
      if (url.hostname !== 'natal.test') return route.abort();
      if (url.pathname === '/static/js/natal-report.js') {
        return route.fulfill({ body: payload.navigation_script, contentType: 'application/javascript' });
      }
      if (url.pathname === '/webapp/api/natal/submit') {
        submitted = route.request().postDataJSON();
        return route.fulfill({ body: '{"ok":true}', contentType: 'application/json' });
      }
      return route.fulfill({ body: payload.html, contentType: 'text/html', headers: payload.headers });
    });
"""
    harness += scenario
    harness += r"""
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=browser_root,
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_natal_report_route_returns_html(monkeypatch):
    from app.web import quart_app

    report = NatalReport(
        report_id="test-report-id-123456",
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
        sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="Натальная карта")],
    )

    async def fake_get_report(report_id: str):
        assert report_id == "test-report-id-123456"
        return report

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()

    response = await client.get("/reports/natal/test-report-id-123456")

    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "<svg" in body
    assert "Натальная карта" in body
    assert response.headers["Content-Type"] == "text/html; charset=utf-8"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    script_tag = re.search(r'<script\b[^>]*src="/static/js/natal-report.js"[^>]*>', body)
    assert script_tag is not None
    nonce = re.search(r'\bnonce="([^"]+)"', script_tag.group())
    assert nonce is not None
    assert f"script-src 'nonce-{nonce.group(1)}'" in response.headers["Content-Security-Policy"]
    assert (
        "'unsafe-inline'" not in response.headers["Content-Security-Policy"].split("script-src ", 1)[1].split(";", 1)[0]
    )
    assert "object-src 'none'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_natal_report_route_returns_404_when_report_missing(monkeypatch):
    from app.web import quart_app

    async def fake_get_report(report_id: str):
        assert report_id == "missing-report-id"
        return None

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()

    response = await client.get("/reports/natal/missing-report-id")

    assert response.status_code == 404
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_natal_report_route_rejects_invalid_report_id_before_storage(monkeypatch):
    from app.web import quart_app

    async def fake_get_report(report_id: str):
        raise AssertionError("invalid report id must not reach storage")

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()

    response = await client.get("/reports/natal/invalid.report.id")

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.browser
async def test_report_links_reveal_closed_sections_and_aliases_in_browser(monkeypatch):
    from app.natal.destiny_matrix import build_destiny_matrix_sections, calculate_destiny_matrix
    from app.natal.svg_renderer import render_chart_svg
    from app.web import quart_app

    matrix = calculate_destiny_matrix("1997-11-09")
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN, houses_available=False, angles_available=False
        ),
        planets=[PlanetPosition(key="mercury", label="Меркурий", longitude=103, sign="Рак", degree_in_sign=13)],
        aspects=[],
        destiny_matrix=matrix,
    )
    report = NatalReport(
        report_id="browser-report-123456",
        user_id=123,
        chart=chart,
        svg=render_chart_svg(chart),
        sections=[
            ReportSection(id="section-summary", title="Общая картина", body_markdown="Искусственный отчет."),
            ReportSection(id="section-thinking", title="Мышление", body_markdown="Текст закрытой секции."),
            *build_destiny_matrix_sections(matrix),
        ],
    )

    async def fake_get_report(_report_id: str):
        return report

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    response = await quart_app.test_client().get("/reports/natal/browser-report-123456")
    assert response.status_code == 200
    _run_browser_regression(
        {
            "html": await response.get_data(as_text=True),
            "headers": {"Content-Security-Policy": response.headers["Content-Security-Policy"]},
        },
        r"""
    await page.goto('http://natal.test/report');
    assert.equal(await page.locator('#section-destiny-periods').evaluate(element => element.open), false);
    await page.locator('.reading-path a[href="#section-destiny-periods"]').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#section-destiny-periods').evaluate(element => element.open), true);
    assert.equal(await page.evaluate(() => document.activeElement.parentElement.id), 'section-destiny-periods');
    await page.locator('#positions > summary').click();
    await page.locator('.position-card[href="#section-thinking"]').click();
    assert.equal(await page.locator('#section-thinking').evaluate(element => element.open), true);
    await page.goto('http://natal.test/report#section-mercury');
    assert.equal(await page.locator('#section-thinking').evaluate(element => element.open), true);
    assert.equal(await page.evaluate(() => document.activeElement.parentElement.id), 'section-thinking');
    await page.evaluate(() => {
      const outer = document.createElement('details');
      outer.id = 'outer';
      outer.innerHTML = '<summary>Outer</summary><details id="nested"><summary>Nested</summary><p id="nested-target">Text</p></details>';
      document.querySelector('main').append(outer);
      window.location.hash = '#nested-target';
    });
    await page.waitForFunction(() => document.getElementById('outer').open && document.getElementById('nested').open);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'nested-target');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.locator('.visual-zoom-toggle').first().click();
    assert.ok(await page.locator('.visual-scroll.is-zoomed svg').evaluate(element => element.getBoundingClientRect().width) >= 800);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
""",
    )


@pytest.mark.asyncio
@pytest.mark.browser
async def test_matrix_form_skips_natal_fields_and_omits_them_from_submission_in_browser():
    from app.web import quart_app

    response = await quart_app.test_client().get("/webapp/natal-form")
    assert response.status_code == 200
    _run_browser_regression(
        {
            "html": await response.get_data(as_text=True),
            "headers": {"Content-Security-Policy": response.headers["Content-Security-Policy"]},
        },
        r"""
    await page.goto('http://natal.test/form');
    assert.equal(await page.locator('.form-slide').first().getAttribute('id'), 'report-slide');
    await page.locator('.option-card[data-value="destiny_matrix"]').click();
    assert.equal(await page.locator('.form-slide:not([hidden])').count(), 3);
    for (let index = 0; index < 12; index++) {
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement.closest('.form-slide')?.getAttribute('aria-hidden') === 'true'), false);
    }
    await page.locator('#next-button').click();
    assert.equal(await page.locator('.form-slide[aria-hidden="false"]').getAttribute('id'), 'date-slide');
    await page.locator('#birth-day').selectOption('09');
    await page.locator('#birth-month').selectOption('11');
    await page.locator('#birth-year').selectOption('1997');
    assert.equal(await page.locator('#progress-text').textContent(), 'Шаг 2 из 3');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.locator('#next-button').click();
    assert.equal(await page.locator('.form-slide[aria-hidden="false"]').getAttribute('id'), 'review-slide');
    assert.equal((await page.locator('#summary').innerText()).includes('Осталось заполнить'), false);
    assert.equal(await page.locator('#submit-button').isEnabled(), true);
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.getElementById('submit-button').textContent === 'Закрыть');
    assert.deepEqual(Object.keys(submitted).sort(), ['birth_date', 'focus', 'report_type']);
    assert.equal(submitted.report_type, 'destiny_matrix');
    await page.goto('http://natal.test/form');
    await page.locator('.option-card[data-value="combined"]').click();
    await page.locator('#next-button').click();
    await page.locator('#birth-day').selectOption('09');
    await page.locator('#birth-month').selectOption('11');
    await page.locator('#birth-year').selectOption('1997');
    await page.locator('#next-button').click();
    await page.locator('[data-group="time_precision"] [data-value="exact"]').click();
    await page.locator('#birth-time').fill('10:30');
    await page.locator('#next-button').click();
    await page.locator('#city-chips .chip:not([data-value="manual"])').first().click();
    await page.locator('#next-button').click();
    for (let index = 0; index < 4; index++) await page.locator('#back-button').click();
    await page.locator('.option-card[data-value="destiny_matrix"]').click();
    await page.locator('#next-button').click();
    await page.locator('#next-button').click();
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.getElementById('submit-button').textContent === 'Закрыть');
    assert.deepEqual(Object.keys(submitted).sort(), ['birth_date', 'focus', 'report_type']);
    assert.equal(submitted.report_type, 'destiny_matrix');
""",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["future_date", "time_range", "minimum_year"])
@pytest.mark.browser
async def test_form_enforces_supported_birth_values_before_next_step_in_browser(case):
    from app.web import quart_app

    response = await quart_app.test_client().get("/webapp/natal-form")
    assert response.status_code == 200
    _run_browser_regression(
        {
            "html": await response.get_data(as_text=True),
            "headers": {"Content-Security-Policy": response.headers["Content-Security-Policy"]},
            "case": case,
        },
        r"""
    await page.clock.setFixedTime(new Date('2026-10-02T12:00:00Z'));
    await page.goto('http://natal.test/form');
    await page.locator(`.option-card[data-value="${payload.case === 'time_range' ? 'combined' : 'destiny_matrix'}"]`).click();
    await page.locator('#next-button').click();
    if (payload.case === 'minimum_year') {
      const years = await page.locator('#birth-year option').evaluateAll(elements => elements.map(element => element.value));
      assert.ok(years.includes('1900'), 'the first supported year must be selectable');
      await page.locator('#birth-day').selectOption('01');
      await page.locator('#birth-month').selectOption('01');
      await page.locator('#birth-year').selectOption('1900');
      assert.equal(await page.locator('#next-button').isEnabled(), true);
    } else if (payload.case === 'future_date') {
      await page.locator('#birth-day').selectOption('31');
      await page.locator('#birth-month').selectOption('12');
      await page.locator('#birth-year').selectOption('2026');
      assert.equal(await page.locator('#next-button').isEnabled(), false);
      assert.equal(await page.locator('#birth-date-error').innerText(), 'Дата рождения не может быть в будущем.');
      assert.equal(await page.locator('#birth-year').getAttribute('aria-invalid'), 'true');
      await page.locator('#birth-year').selectOption('1997');
      assert.equal(await page.locator('#next-button').isEnabled(), true);
      assert.equal(await page.locator('#birth-date-error').isVisible(), false);
    } else {
      await page.locator('#birth-day').selectOption('09');
      await page.locator('#birth-month').selectOption('11');
      await page.locator('#birth-year').selectOption('1997');
      await page.locator('#next-button').click();
      await page.locator('[data-group="time_precision"] [data-value="range"]').click();
      await page.locator('#birth-time-start').fill('11:00');
      await page.locator('#birth-time-end').fill('10:00');
      assert.equal(await page.locator('#next-button').isEnabled(), false);
      assert.equal(await page.locator('#birth-time-error').innerText(), 'Диапазон времени должен заканчиваться позже начала.');
      await page.locator('#birth-time-end').fill('11:00');
      assert.equal(await page.locator('#next-button').isEnabled(), false);
      await page.locator('#birth-time-end').fill('12:00');
      assert.equal(await page.locator('#next-button').isEnabled(), true);
      assert.equal(await page.locator('#birth-time-error').isVisible(), false);
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
""",
    )

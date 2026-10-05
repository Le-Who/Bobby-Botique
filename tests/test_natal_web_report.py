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
    static_root = Path(__file__).resolve().parents[1] / "app/static"
    payload["assets"] = {
        f"/static/{path.relative_to(static_root).as_posix()}": path.read_text(encoding="utf-8")
        for path in [
            static_root / "js/natal-theme.js",
            static_root / "js/natal-report.js",
            static_root / "css/natal-theme.css",
            static_root / "css/natal-report.css",
        ]
        if path.exists()
    }
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 320, height: 844 }, reducedMotion: 'reduce',
      javaScriptEnabled: payload.javascript !== false
    });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let submitted;
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'telegram.org') return route.fulfill({ body: '', contentType: 'application/javascript' });
      if (url.hostname !== 'natal.test') return route.abort();
      if (url.pathname.startsWith('/static/')) {
        const body = payload.assets[url.pathname];
        return route.fulfill({
          body: body || '', status: body === undefined ? 404 : 200,
          contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript'
        });
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
    for script in re.findall(r"<script\b[^>]*>", body):
        assert f'nonce="{nonce.group(1)}"' in script


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


@pytest.mark.asyncio
@pytest.mark.browser
@pytest.mark.parametrize(
    ("host_theme", "system_theme", "initial_theme"),
    [("dark", "light", "dark"), ("light", "dark", "light"), (None, "dark", "dark")],
)
async def test_form_theme_follows_host_or_system_without_resetting_input_in_browser(
    host_theme, system_theme, initial_theme
):
    from app.web import quart_app

    response = await quart_app.test_client().get("/webapp/natal-form")
    _run_browser_regression(
        {
            "html": await response.get_data(as_text=True),
            "headers": {"Content-Security-Policy": response.headers["Content-Security-Policy"]},
            "host_theme": host_theme,
            "system_theme": system_theme,
            "initial_theme": initial_theme,
        },
        r"""
    await page.emulateMedia({ colorScheme: payload.system_theme });
    await page.addInitScript(({ hostTheme }) => {
      const handlers = {};
      window.Telegram = { WebApp: {
        initData: hostTheme ? 'synthetic-test-session' : '',
        colorScheme: hostTheme || 'light', themeParams: {},
        onEvent(name, callback) { handlers[name] = callback; },
        ready() {}, expand() {}, close() {}
      } };
      window.changeHostTheme = theme => {
        window.Telegram.WebApp.colorScheme = theme;
        handlers.themeChanged?.();
      };
    }, { hostTheme: payload.host_theme });
    await page.goto('http://natal.test/form');
    assert.equal(await page.locator('html').getAttribute('data-natal-theme'), payload.initial_theme);
    await page.locator('.option-card[data-value="combined"]').click();
    await page.locator('#next-button').click();
    await page.locator('#birth-day').selectOption('09');
    await page.locator('#birth-month').selectOption('11');
    await page.locator('#birth-year').selectOption('1997');
    await page.locator('#next-button').click();
    await page.locator('[data-group="time_precision"] [data-value="exact"]').click();
    await page.locator('#birth-time').fill('10:30');
    const nextTheme = payload.initial_theme === 'dark' ? 'light' : 'dark';
    if (payload.host_theme) {
      await page.emulateMedia({ colorScheme: payload.system_theme === 'dark' ? 'light' : 'dark' });
      assert.equal(await page.locator('html').getAttribute('data-natal-theme'), payload.initial_theme);
      await page.evaluate(theme => window.changeHostTheme(theme), nextTheme);
    } else {
      await page.emulateMedia({ colorScheme: nextTheme });
    }
    await page.waitForFunction(theme => document.documentElement.dataset.natalTheme === theme, nextTheme);
    assert.equal(await page.locator('#birth-time').inputValue(), '10:30');
    assert.equal(await page.locator('.form-slide[aria-hidden="false"]').getAttribute('id'), 'time-slide');
    assert.equal(await page.locator('[data-group="time_precision"] [data-value="exact"]').getAttribute('aria-pressed'), 'true');
    const columns = await page.locator('.precision-grid').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length);
    assert.equal(columns, nextTheme === 'dark' ? 2 : 1);
    for (const width of [320, 390, 900]) {
      await page.setViewportSize({ width, height: 844 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const iconOffsets = await page.locator('.cancel svg, .brand-mark svg, .rail-marker svg, .precision-icon svg').evaluateAll(icons => icons.filter(icon => icon.getClientRects().length).map(svg => {
        const cell = svg.parentElement.getBoundingClientRect();
        const bounds = svg.getBBox();
        const icon = new DOMPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2).matrixTransform(svg.getScreenCTM());
        return { dx: icon.x - cell.x - cell.width / 2, dy: icon.y - cell.y - cell.height / 2 };
      }));
      assert.ok(iconOffsets.every(({ dx, dy }) => Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5), JSON.stringify(iconOffsets));
      const clockOffset = await page.locator('.time-input-shell').evaluate(shell => {
        const icon = shell.querySelector('svg').getBoundingClientRect();
        const input = shell.querySelector('input').getBoundingClientRect();
        return icon.y + icon.height / 2 - input.y - input.height / 2;
      });
      assert.ok(Math.abs(clockOffset) < 0.5, String(clockOffset));
    }
""",
    )


@pytest.mark.asyncio
@pytest.mark.browser
async def test_form_blocks_duplicate_pending_submission_and_recovers_after_error_in_browser():
    from app.web import quart_app

    response = await quart_app.test_client().get("/webapp/natal-form")
    _run_browser_regression(
        {"html": await response.get_data(as_text=True)},
        r"""
    let requests = 0;
    let releaseResponse;
    const pendingResponse = new Promise(resolve => { releaseResponse = resolve; });
    await page.route('**/webapp/api/natal/submit', async route => {
      requests += 1;
      if (requests === 1) {
        await pendingResponse;
        await route.fulfill({ status: 503, body: '{"ok":false,"detail":"Проверочный отказ"}', contentType: 'application/json' });
      } else {
        await route.fulfill({ body: '{"ok":true}', contentType: 'application/json' });
      }
    });
    await page.goto('http://natal.test/form');
    await page.locator('.option-card[data-value="destiny_matrix"]').click();
    await page.locator('#next-button').click();
    await page.locator('#birth-day').selectOption('09');
    await page.locator('#birth-month').selectOption('11');
    await page.locator('#birth-year').selectOption('1997');
    await page.locator('#next-button').click();
    assert.ok((await page.locator('#summary').innerText()).includes('9 ноября 1997'));
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.getElementById('submit-button').textContent === 'Отправляю...');
    await page.locator('#birth-year').dispatchEvent('change');
    assert.equal(await page.locator('#submit-button').isDisabled(), true);
    await page.locator('#natal-form').dispatchEvent('submit');
    releaseResponse();
    await page.waitForFunction(() => !document.getElementById('error-box').hidden);
    assert.equal(requests, 1);
    assert.equal(await page.locator('#error-box').innerText(), 'Проверочный отказ');
    assert.equal(await page.locator('#submit-button').isEnabled(), true);
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.getElementById('submit-button').textContent === 'Закрыть');
    assert.equal(requests, 2);
""",
    )


@pytest.mark.asyncio
@pytest.mark.browser
async def test_form_clears_manual_city_when_country_changes_and_focuses_next_in_browser():
    from app.web import quart_app

    response = await quart_app.test_client().get("/webapp/natal-form")
    _run_browser_regression(
        {"html": await response.get_data(as_text=True)},
        r"""
    await page.goto('http://natal.test/form');
    await page.locator('.option-card[data-value="combined"]').click();
    await page.locator('#next-button').click();
    await page.locator('#birth-day').selectOption('09');
    await page.locator('#birth-month').selectOption('11');
    await page.locator('#birth-year').selectOption('1997');
    await page.locator('#next-button').click();
    await page.locator('[data-group="time_precision"] [data-value="unknown"]').click();
    assert.equal(await page.locator('#single-time-block').isVisible(), false);
    assert.equal(await page.locator('#range-time-block').isVisible(), false);
    await page.locator('#next-button').click();
    await page.locator('#city-chips .chip[data-value="manual"]').click();
    await page.locator('#manual-city').fill('Тестовый город');
    assert.equal(await page.locator('#next-button').isEnabled(), true);
    await page.locator('#country-chips .chip:not([data-value="manual"])').nth(1).click();
    assert.equal(await page.locator('#manual-city').inputValue(), '');
    assert.equal(await page.locator('#next-button').isEnabled(), false);
    await page.locator('#city-chips .chip:not([data-value="manual"])').first().click();
    await page.waitForFunction(() => document.activeElement.id === 'next-button');
    await page.locator('#next-button').click();
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.getElementById('submit-button').textContent === 'Закрыть');
    assert.equal(submitted.time_precision, 'unknown');
    assert.equal('birth_time' in submitted, false);
    assert.equal('city' in submitted, false);
""",
    )


@pytest.mark.asyncio
@pytest.mark.browser
@pytest.mark.parametrize("precision", [TimePrecision.EXACT, TimePrecision.UNKNOWN])
@pytest.mark.parametrize("javascript", [True, False])
async def test_report_theme_keeps_real_diagrams_and_precision_limits_in_browser(monkeypatch, precision, javascript):
    from app.natal.destiny_matrix import build_destiny_matrix_sections, calculate_destiny_matrix
    from app.natal.svg_renderer import render_chart_svg
    from app.web import quart_app

    matrix = calculate_destiny_matrix("1997-11-09")
    known_time = precision != TimePrecision.UNKNOWN
    chart = ChartData(
        input_quality=InputQuality(time_precision=precision, houses_available=known_time, angles_available=known_time),
        planets=[
            PlanetPosition(key="sun", label="Солнце", longitude=45, sign="Телец", degree_in_sign=15),
            PlanetPosition(key="moon", label="Луна", longitude=195, sign="Весы", degree_in_sign=15),
        ],
        aspects=[],
        angles={"ascendant": 170},
        destiny_matrix=matrix,
    )
    report = NatalReport(
        report_id="theme-browser-report-123456",
        user_id=123,
        chart=chart,
        svg=render_chart_svg(chart),
        sections=[
            ReportSection(id="section-identity", title="Личность", body_markdown="Синтетический пример."),
            ReportSection(id="section-emotions", title="Эмоции", body_markdown="Синтетический пример."),
            *build_destiny_matrix_sections(matrix),
        ],
    )

    async def fake_get_report(_report_id: str):
        return report

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    response = await quart_app.test_client().get("/reports/natal/theme-browser-report-123456")
    _run_browser_regression(
        {
            "html": await response.get_data(as_text=True),
            "headers": {"Content-Security-Policy": response.headers["Content-Security-Policy"]},
            "javascript": javascript,
            "known_time": known_time,
        },
        r"""
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto('http://natal.test/report');
    assert.equal(await page.locator('.chart-stage svg').count(), 1);
    assert.equal(await page.locator('.matrix-stage svg').count(), 1);
    assert.equal(await page.locator('[data-point="ascendant"]').count(), payload.known_time ? 1 : 0);
    assert.equal(await page.locator('.quick-position').count(), payload.known_time ? 3 : 2);
    assert.equal(await page.locator('.calculation-note').count(), payload.known_time ? 0 : 1);
    const chartStop = page.locator('.chart-stage stop').first();
    const matrixStop = page.locator('.matrix-stage stop').first();
    assert.equal(await chartStop.evaluate(element => getComputedStyle(element).stopColor), 'rgb(16, 54, 61)');
    assert.equal(await matrixStop.evaluate(element => getComputedStyle(element).stopColor), 'rgb(16, 54, 61)');
    await page.locator('#positions > summary').click();
    assert.equal(await page.locator('#positions').evaluate(element => element.open), true);
    if (payload.javascript) {
      await page.locator('.report-nav a[href="#full-reading"]').click();
      await page.waitForFunction(() => document.querySelector('.report-nav a[href="#full-reading"]').getAttribute('aria-current') === 'location');
      assert.equal(await page.locator('.report-nav a[href="#full-reading"]').getAttribute('aria-current'), 'location');
      await page.emulateMedia({ colorScheme: 'light' });
      await page.waitForFunction(() => document.documentElement.dataset.natalTheme === 'light');
      assert.equal(await page.locator('#positions').evaluate(element => element.open), true);
      assert.equal(await chartStop.evaluate(element => getComputedStyle(element).stopColor), 'rgb(247, 253, 255)');
      assert.equal(await matrixStop.evaluate(element => getComputedStyle(element).stopColor), 'rgb(247, 253, 255)');
    } else {
      assert.equal(await page.locator('.visual-zoom-toggle').first().isVisible(), false);
    }
    await page.emulateMedia({ forcedColors: 'active' });
    const disclosure = page.locator('#positions .disclosure-icon');
    assert.equal(await disclosure.isVisible(), true);
    assert.equal(await disclosure.locator('.disclosure-vertical').evaluate(element => getComputedStyle(element).display), 'none');
    assert.notEqual(await disclosure.locator('path').first().evaluate(element => getComputedStyle(element).stroke), 'none');
    await page.locator('#positions > summary').click();
    assert.notEqual(await disclosure.locator('.disclosure-vertical').evaluate(element => getComputedStyle(element).display), 'none');
    await page.emulateMedia({ forcedColors: 'none' });
    for (const width of [320, 390, 1100]) {
      await page.setViewportSize({ width, height: 844 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const glyphOffsets = await page.locator('.chart-stage svg a').evaluateAll(links => links.map(link => {
        const circle = link.querySelector('circle');
        const text = link.querySelector('text');
        const bounds = text.getBBox();
        const matrix = text.getScreenCTM();
        const glyph = new DOMPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2).matrixTransform(matrix);
        const center = new DOMPoint(circle.cx.baseVal.value, circle.cy.baseVal.value).matrixTransform(matrix);
        return { dx: glyph.x - center.x, dy: glyph.y - center.y };
      }));
      // Text-anchor centers the advance width; natural side bearings may differ by a subpixel.
      assert.ok(glyphOffsets.every(({ dx, dy }) => Math.abs(dx) < 1 && Math.abs(dy) < 0.5), JSON.stringify(glyphOffsets));
      const iconOffsets = await page.locator('.brand-mark, .point-symbol, .entry-symbol, .point-arrow, .entry-arrow, .disclosure-icon').evaluateAll(cells => cells.filter(cell => cell.getClientRects().length).map(cell => {
        const svg = cell.querySelector('svg');
        const box = cell.getBoundingClientRect();
        const bounds = svg.getBBox();
        const icon = new DOMPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2).matrixTransform(svg.getScreenCTM());
        return { dx: icon.x - box.x - box.width / 2, dy: icon.y - box.y - box.height / 2 };
      }));
      assert.ok(iconOffsets.every(({ dx, dy }) => Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5), JSON.stringify(iconOffsets));
    }
""",
    )

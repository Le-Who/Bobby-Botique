"""Real Chromium checks for the pair questionnaire, with external APIs isolated."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
COPY = {
    "title": "Совместимость",
    "intro": "Заполните данные двух партнёров.",
    "first": "Первый партнёр",
    "second": "Второй партнёр",
    "date": "Дата рождения",
    "day": "День",
    "month": "Месяц",
    "year": "Год",
    "time": "Время рождения",
    "exact": "Точное",
    "approximate": "Примерное",
    "range": "Диапазон",
    "unknown": "Не знаю",
    "time_start": "Начало",
    "time_end": "Конец",
    "place": "Место рождения",
    "place_unknown": "Место неизвестно",
    "country": "Страна",
    "city": "Город",
    "city_hint": "Выберите город из списка.",
    "next": "Продолжить",
    "back": "Назад",
    "review": "Проверьте данные",
    "submit": "Рассчитать совместимость",
    "cancel": "Отмена",
    "date_error": "Выберите существующую дату рождения.",
    "time_error": "Проверьте время рождения.",
    "place_error": "Выберите город из списка.",
    "country_error": "Выберите страну из списка.",
    "form_error": "Не удалось отправить. Попробуйте ещё раз.",
    "auth_error": "Откройте форму в Telegram.",
    "accepted": "Данные приняты",
    "accepted_note": "Результат придёт в личный чат",
    "step": "Шаг",
    "no_results": "Ничего не найдено",
}


def _run_browser(scenario: str, *, lang: str = "ru", init_data: str = "test-init-data") -> None:
    browser_root = ROOT / "tests/browser"
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
    environment = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
    html = environment.get_template("compatibility_form.html").render(
        compatibility_options={
            "pair": "compat_m7_f10",
            "lang": lang,
            "first_label": "Мужчина · Весы",
            "second_label": "Женщина · Водолей",
            "copy": COPY,
        },
        csp_nonce="test-nonce",
    )
    assets = {
        f"/static/{name}": (ROOT / "app/static" / name).read_text(encoding="utf-8")
        for name in [
            "js/natal-theme.js",
            "css/natal-theme.css",
            "js/compatibility-form.js",
            "css/compatibility-form.css",
        ]
    }
    payload = {"html": html, "assets": assets, "initData": init_data}
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 844 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(3000);
    const errors = [];
    const requests = [];
    let submitHandler;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(initData => {
      window.Telegram = { WebApp: {
        initData, ready() {}, expand() {}, close() { window.formClosed = true; },
        onEvent(name, callback) { if (name === 'themeChanged') window.themeHandler = callback; },
        colorScheme: 'dark', themeParams: {},
        BackButton: { show() {}, hide() {}, onClick() {} },
      }};
    }, payload.initData);
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'telegram.org') return route.fulfill({ body: '', contentType: 'application/javascript' });
      if (url.hostname !== 'compatibility.test') return route.abort();
      if (url.pathname.startsWith('/static/')) {
        return route.fulfill({ body: payload.assets[url.pathname] || '',
          contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript' });
      }
      if (url.pathname.startsWith('/webapp/api/compatibility/')) {
        requests.push({ path: url.pathname, query: url.search, headers: route.request().headers(),
          body: route.request().postDataJSON() });
        if (url.pathname.endsWith('/submit')) {
          if (submitHandler) return submitHandler(route);
          return route.fulfill({ status: 202, json: { ok: true, status: 'accepted' } });
        }
        const items = url.pathname.endsWith('/countries')
          ? [{ id: 'UA', label: 'Украина' }, { id: 'PL', label: 'Польша' }]
          : [{ id: '703448', label: 'Киев' }, { id: '698740', label: 'Одесса' }];
        return route.fulfill({ json: { items } });
      }
      return route.fulfill({ body: payload.html, contentType: 'text/html' });
    });
    const fillDate = async (partner, day = '6', month = '9', year = '1994') => {
      await page.locator(`#${partner}-day`).selectOption(day);
      await page.locator(`#${partner}-month`).selectOption(month);
      await page.locator(`#${partner}-year`).selectOption(year);
    };
    const dateOnlyReview = async () => {
      await fillDate('first');
      await page.locator('#next-button').click();
      await fillDate('second', '29', '2', '1996');
      await page.locator('#next-button').click();
    };
    await page.goto('http://compatibility.test/form');
"""
    harness += scenario
    harness += r"""
    assert.deepEqual(errors, []);
    await page.locator('#cancel-button').click();
    assert.equal(await page.evaluate(() => window.formClosed), true);
  } finally { await browser.close(); }
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


@pytest.mark.browser
def test_date_only_pair_has_editable_review_and_waits_for_server_acceptance():
    # Catches premature success, extra birth fields, double submission, or lost edits.
    _run_browser(r"""
    assert.equal(await page.title(), 'Совместимость');
    assert.equal(await page.locator('h1').innerText(), 'Совместимость');
    await dateOnlyReview();
    assert.equal(await page.locator('#compatibility-review').isVisible(), true);
    assert.match(await page.locator('#review-first').innerText(), /6.*1994/);
    assert.match(await page.locator('#review-second').innerText(), /29.*1996/);
    await page.locator('[data-edit="first"]').click();
    assert.equal(await page.locator('#first-year').inputValue(), '1994');
    await page.locator('#first-day').selectOption('7');
    await page.locator('#next-button').click();
    await page.locator('#next-button').click();
    let release;
    submitHandler = route => new Promise(resolve => { release = async () => {
      await route.fulfill({ status: 202, json: { ok: true, status: 'accepted' } }); resolve();
    }; });
    await page.locator('#submit-button').click();
    await page.waitForFunction(() => document.querySelector('#submit-button').disabled);
    assert.equal(await page.locator('#submission-status').isVisible(), false);
    await page.locator('#compatibility-form').evaluate(form => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    assert.equal(requests.filter(request => request.path.endsWith('/submit')).length, 1);
    const submission = requests.find(request => request.path.endsWith('/submit'));
    assert.equal(submission.headers.authorization, 'tma test-init-data');
    assert.deepEqual(submission.body, {
      pair: 'compat_m7_f10', first: { birth_date: '1994-09-07', time_precision: 'unknown' },
      second: { birth_date: '1996-02-29', time_precision: 'unknown' }
    });
    await release();
    await page.locator('#submission-status').waitFor({ state: 'visible' });
    assert.match(await page.locator('#submission-status').innerText(), /Результат придёт в личный чат/);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.equal(new URL(page.url()).search, '');
""")


@pytest.mark.browser
@pytest.mark.parametrize("precision", ["exact", "approximate"])
def test_known_time_requires_selected_place_and_mixed_pair_preserves_precision(precision):
    # Catches typed city recognition, dropped known time, and stale city after country edits.
    _run_browser(
        r"""
    await fillDate('first');
    await page.locator('[name="first-time-precision"][value="__PRECISION__"]').check();
    await page.locator('#first-time').fill('08:15');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#partner-first').isVisible(), true);
    assert.equal(await page.locator('#first-country').getAttribute('aria-invalid'), 'true');
    await page.locator('#first-country').fill('Ук');
    await page.locator('#first-country-results button').first().click();
    await page.locator('#first-city').fill('Ки');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#partner-first').isVisible(), true);
    await page.locator('#first-city-results button').first().click();
    await page.locator('#first-country').fill('По');
    assert.equal(await page.locator('#first-city').inputValue(), '');
    await page.locator('#first-country-results button').nth(1).click();
    await page.locator('#first-city').fill('Ки');
    await page.locator('#first-city-results button').first().click();
    await page.locator('#first-country').fill('Ук');
    await page.locator('#first-country-results button').first().click();
    await page.locator('#first-city').fill('Ки');
    await page.locator('#first-city-results button').first().click();
    await page.locator('#next-button').click();
    await fillDate('second');
    await page.locator('#next-button').click();
    assert.match(await page.locator('#review-first').innerText(), /08:15/);
    assert.match(await page.locator('#review-second').innerText(), /Не знаю/);
    await page.locator('#submit-button').click();
    await page.locator('#submission-status').waitFor({ state: 'visible' });
    const submitted = requests.find(request => request.path.endsWith('/submit')).body;
    assert.deepEqual(submitted.first, {
      birth_date: '1994-09-06', time_precision: '__PRECISION__', birth_time: '08:15',
      country_code: 'UA', city_geoname_id: '703448'
    });
    assert.deepEqual(submitted.second, { birth_date: '1994-09-06', time_precision: 'unknown' });
    assert.ok(requests.filter(request => request.path.endsWith('/cities'))
      .every(request => new URLSearchParams(request.query).has('country')));
    assert.ok(requests.every(request => request.headers.authorization === 'tma test-init-data'));
""".replace("__PRECISION__", precision)
    )


@pytest.mark.browser
def test_submission_error_retains_inputs_and_allows_one_retry():
    # Catches discarded drafts, false accepted messages on errors, and disabled retries.
    _run_browser(r"""
    await dateOnlyReview();
    submitHandler = route => route.fulfill({ status: 422, json: { error: 'invalid_input', detail: '<img src=x> Проверка' } });
    await page.locator('#submit-button').click();
    await page.locator('#form-error').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#submission-status').isVisible(), false);
    assert.equal(await page.locator('#submit-button').isEnabled(), true);
    assert.equal(await page.locator('#form-error img').count(), 0);
    await page.locator('#back-button').click();
    assert.equal(await page.locator('#second-day').inputValue(), '29');
    assert.equal(await page.locator('#second-year').inputValue(), '1996');
    await page.locator('#next-button').click();
    submitHandler = undefined;
    await page.locator('#submit-button').click();
    await page.locator('#submission-status').waitFor({ state: 'visible' });
    assert.equal(requests.filter(request => request.path.endsWith('/submit')).length, 2);
""")


@pytest.mark.browser
def test_validation_refocus_keeps_city_suggestions_open():
    _run_browser(r"""
    await fillDate('first');
    await page.locator('[name="first-time-precision"][value="approximate"]').check();
    await page.locator('#first-time').fill('08:15');
    await page.locator('#first-country').fill('Ук');
    await page.locator('#first-country-results button').first().click();
    await page.locator('#first-city').fill('Ки');
    await page.locator('#first-city-results').waitFor({ state: 'visible' });
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#first-city').evaluate(node => node === document.activeElement), true);
    // A queued blur close must not hide results after validation returns focus.
    await page.waitForTimeout(200);
    assert.equal(await page.locator('#first-city-results').isVisible(), true);
    await page.locator('#first-city-results button').first().click();
    assert.equal(await page.locator('#first-city').inputValue(), 'Киев');
""")


@pytest.mark.browser
def test_form_has_no_horizontal_overflow_and_theme_changes_keep_dates():
    # Catches mobile clipping and state resets when the host theme changes.
    _run_browser(r"""
    await fillDate('first');
    for (const scheme of ['dark', 'light']) {
      await page.evaluate(scheme => { window.Telegram.WebApp.colorScheme = scheme; window.themeHandler(); }, scheme);
      assert.equal(await page.locator('html').getAttribute('data-natal-theme'), scheme);
      for (const width of [320, 390, 1100]) {
        await page.setViewportSize({ width, height: 844 });
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      }
      assert.equal(await page.locator('#first-year').inputValue(), '1994');
      await page.locator('#next-button').click();
      await fillDate('second');
      await page.locator('#next-button').click();
      await page.setViewportSize({ width: 320, height: 844 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      await page.locator('[data-edit="first"]').click();
    }
""")


@pytest.mark.browser
def test_missing_telegram_auth_never_reports_success_or_sends_birth_data():
    # Catches submitting unsigned drafts from an ordinary browser tab.
    _run_browser(
        r"""
    await dateOnlyReview();
    await page.locator('#submit-button').click();
    await page.locator('#form-error').waitFor({ state: 'visible' });
    assert.match(await page.locator('#form-error').innerText(), /Telegram/);
    assert.equal(await page.locator('#submission-status').isVisible(), false);
    assert.equal(requests.length, 0);
""",
        init_data="",
    )


@pytest.mark.browser
def test_invalid_calendar_date_stays_on_partner_and_focuses_error():
    # Catches accepting calendar rollovers (31 April, non-leap February) as valid dates.
    _run_browser(r"""
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#first-day').getAttribute('aria-invalid'), 'true');
    await fillDate('first', '31', '4', '1994');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#partner-first').isVisible(), true);
    assert.equal(await page.locator('#first-date-error').isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'first-year');
    await fillDate('first', '29', '2', '1995');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#partner-first').isVisible(), true);
    await page.locator('#first-year').selectOption('1996');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#partner-second').isVisible(), true);
    assert.equal(requests.length, 0);
""")


@pytest.mark.browser
def test_range_and_optional_place_use_selected_catalog_ids_with_keyboard():
    # Catches reversed ranges, inaccessible result selection, and silently dropped optional place.
    _run_browser(r"""
    await fillDate('first');
    await page.locator('[name="first-time-precision"][value="range"]').check();
    await page.locator('#first-time-start').fill('10:00');
    await page.locator('#first-time-end').fill('09:00');
    await page.locator('#next-button').click();
    assert.equal(await page.locator('#first-time-error').isVisible(), true);
    await page.locator('#first-time-end').fill('11:00');
    await page.locator('#first-country').fill('Ук');
    await page.locator('#first-country-results button').first().waitFor();
    await page.locator('#first-country').press('ArrowDown');
    await page.locator('#first-country').press('Enter');
    await page.locator('#first-city').fill('Ки');
    await page.locator('#first-city-results button').first().click();
    await page.locator('#next-button').click();
    await fillDate('second');
    await page.locator('#second-place-unknown').uncheck();
    await page.locator('#second-country').fill('Ук');
    await page.locator('#second-country-results button').first().click();
    await page.locator('#second-city').fill('Од');
    await page.locator('#second-city-results button').nth(1).click();
    await page.locator('#next-button').click();
    assert.match(await page.locator('#review-second').innerText(), /Одесса/);
    await page.locator('#submit-button').click();
    await page.locator('#submission-status').waitFor({ state: 'visible' });
    assert.deepEqual(requests.find(request => request.path.endsWith('/submit')).body, {
      pair: 'compat_m7_f10',
      first: { birth_date: '1994-09-06', time_precision: 'range', birth_time_range_start: '10:00',
        birth_time_range_end: '11:00', country_code: 'UA', city_geoname_id: '703448' },
      second: { birth_date: '1994-09-06', time_precision: 'unknown', country_code: 'UA', city_geoname_id: '698740' }
    });
""")


@pytest.mark.browser
def test_explicit_unknown_place_omits_retained_selection_without_erasing_the_draft():
    # Catches submitting stale city despite the explicit skip choice, or losing it on back.
    _run_browser(r"""
    await fillDate('first');
    await page.locator('#first-place-unknown').uncheck();
    await page.locator('#first-country').fill('Ук');
    await page.locator('#first-country-results button').first().click();
    await page.locator('#first-city').fill('Ки');
    await page.locator('#first-city-results button').first().click();
    await page.locator('#first-place-unknown').check();
    await page.locator('#next-button').click();
    await fillDate('second');
    await page.locator('#next-button').click();
    assert.match(await page.locator('#review-first').innerText(), /Место неизвестно/);
    await page.locator('[data-edit="first"]').click();
    await page.locator('#first-place-unknown').uncheck();
    assert.equal(await page.locator('#first-city').inputValue(), 'Киев');
    await page.locator('#first-place-unknown').check();
    await page.locator('#next-button').click();
    await page.locator('#next-button').click();
    await page.locator('#submit-button').click();
    await page.locator('#submission-status').waitFor({ state: 'visible' });
    assert.deepEqual(requests.find(request => request.path.endsWith('/submit')).body.first,
      { birth_date: '1994-09-06', time_precision: 'unknown' });
""")


@pytest.mark.browser
def test_success_shaped_response_without_accepted_acknowledgement_keeps_form_editable():
    # Catches treating any HTTP 2xx or ok flag as acceptance.
    _run_browser(r"""
    await dateOnlyReview();
    submitHandler = route => route.fulfill({ status: 200, json: { ok: true } });
    await page.locator('#submit-button').click();
    await page.locator('#form-error').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#submission-status').isVisible(), false);
    assert.equal(await page.locator('#submit-button').isEnabled(), true);
    assert.equal(await page.locator('#compatibility-review').isVisible(), true);
""")

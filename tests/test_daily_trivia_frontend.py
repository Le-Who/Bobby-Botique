"""Exercise the real Trivia template with isolated HTTP fixtures in Chromium."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def _run_browser(scenario: str, *, completed: bool = False, telegram: bool = True, long_option: bool = False) -> None:
    browser_root = ROOT / "tests/browser"
    node = shutil.which("node")
    required = os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1"
    if not node:
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
    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
    html = env.get_template("daily_trivia.html").render(bot_username="test_bot", current_daily_game="trivia")
    question = {
        "id": 1,
        "topic": "Наука",
        "question": "Какая планета Солнечной системы находится ближе всего к Солнцу?",
        "options": ["Меркурий", "Венера", "Земля", "Марс"],
        "correct_index": 0,
        "explanation": "Меркурий — ближайшая к Солнцу планета.",
    }
    if long_option:
        question["options"][0] = "Оченьдлинныйнеразрывныйвариантответадляпроверкипереноса" * 4
    data = {
        "date": "2026-10-04",
        "revision_id": 42,
        "questions": [dict(question, id=index) for index in range(5)],
        "super_questions": [dict(question, id=index + 5) for index in range(3)],
        "user_result": None,
        "user_super_result": None,
    }
    if completed:
        answers = [{"question_index": index, "selected_index": 0, "is_correct": True} for index in range(5)]
        data["user_result"] = {"status": "completed", "final_score": 2500, "correct_count": 5, "answers": answers}
        data["user_super_result"] = {
            "status": "completed",
            "delta_score": 1000,
            "correct_count": 2,
            "answers": answers[:3],
        }
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 568 }, reducedMotion: 'reduce' });
    page.setDefaultTimeout(5000);
    if (payload.telegram) await page.addInitScript(() => {
      window.Telegram = { WebApp: { initData: 'offline-test-auth', expand() {}, ready() {} } };
    });
    const errors = [];
    const submissions = [];
    let todayRequests = 0;
    let releaseAnswer;
    let answerMode = 'ok';
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'trivia.test') return route.abort();
      if (url.pathname.endsWith('/trivia/today')) {
        todayRequests++;
        return route.fulfill({ json: payload.data });
      }
      if (url.pathname.includes('/submit_')) {
        submissions.push({ path: url.pathname, body: route.request().postDataJSON() });
        if (answerMode === 'pending') await new Promise(resolve => { releaseAnswer = resolve; });
        return route.fulfill({ status: answerMode === 'fail' ? 503 : 200, json: { success: !['fail', 'reject'].includes(answerMode), already_completed: answerMode === 'already' } });
      }
      if (url.pathname.startsWith('/static/')) {
        return route.fulfill({ body: payload.assets[url.pathname] || '', contentType: url.pathname.endsWith('.js') ? 'application/javascript' : 'text/css' });
      }
      return route.fulfill({ body: payload.html, contentType: 'text/html' });
    });
    await page.goto('https://trivia.test/dailytrivia');
"""
    harness += scenario
    harness += r"""
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    assets = {
        f"/static/{path}": (ROOT / "app/static" / path).read_text(encoding="utf-8")
        for path in ("css/daily-switcher.css", "js/daily-switcher.js")
    }
    result = subprocess.run(
        [node, "-e", harness],
        cwd=browser_root,
        input=json.dumps({"html": html, "data": data, "assets": assets, "telegram": telegram}),
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.browser
def test_trivia_answer_waits_for_save_before_next_question():
    _run_browser(r"""
    await page.locator('#optionsGrid button').first().waitFor();
    answerMode = 'pending';
    await page.locator('#optionsGrid button').first().click();
    assert.equal(await page.locator('#nextBtn').isDisabled(), true, 'Do not advance while the answer is saving');
    assert.match(await page.locator('#answerStatus').innerText(), /Сохраняем/);
    assert.equal(await page.locator('#quizProgress').getAttribute('aria-valuenow'), '1');
    assert.equal(await page.locator('#optionsGrid button:enabled').count(), 0);
    await page.waitForFunction(() => document.querySelector('#explanationCard').offsetHeight > 0);
    releaseAnswer();
    await page.waitForFunction(() => !document.querySelector('#nextBtn').disabled);
    assert.match(await page.locator('#answerStatus').innerText(), /сохран/);
    await page.locator('#nextBtn').click();
    assert.match(await page.locator('#qCounter').innerText(), /2\/5/);
    assert.equal(await page.locator('#questionText').evaluate(el => el === document.activeElement), true);
    assert.equal(submissions.length, 1);
    assert.equal(submissions[0].body.revision_id, 42);
    """)


@pytest.mark.browser
@pytest.mark.parametrize("failure_mode", ["fail", "reject"])
def test_trivia_failed_save_is_visible_and_does_not_advance(failure_mode):
    _run_browser(
        r"""
    answerMode = 'FAILURE_MODE';
    await page.locator('#optionsGrid button').first().click();
    await page.locator('#saveRecoveryBtn').waitFor({ state: 'visible' });
    assert.match(await page.locator('#answerStatus').innerText(), /Не удалось/);
    assert.equal(await page.locator('#nextBtn').isDisabled(), true);
    assert.equal(await page.locator('#optionsGrid button:enabled').count(), 0);
    assert.equal(submissions.length, 1);
    """.replace("FAILURE_MODE", failure_mode)
    )


@pytest.mark.browser
def test_trivia_completion_and_supergame_have_correct_progress_and_actions():
    _run_browser(r"""
    for (let i = 0; i < 5; i++) {
      await page.locator('#optionsGrid button').first().click();
      await page.waitForFunction(() => !document.querySelector('#nextBtn').disabled);
      if (i === 4) assert.match(await page.locator('#nextBtn').innerText(), /результат/);
      await page.locator('#nextBtn').click();
    }
    assert.equal(await page.locator('#quizProgress').getAttribute('aria-valuenow'), '5');
    assert.match(await page.locator('#qCounter').innerText(), /5\/5/);
    await page.locator('#superStartBtn').click();
    for (let i = 0; i < 3; i++) {
      await page.locator('#superOptionsGrid button').nth(1).click();
      await page.waitForFunction(() => !document.querySelector('#superNextBtn').disabled);
      assert.match(await page.locator('#superAnswerOutcome').innerText(), /Неверно/);
      await page.locator('#superNextBtn').click();
    }
    assert.equal(await page.locator('#quizProgress').getAttribute('aria-valuenow'), '3');
    assert.equal(await page.locator('#quizProgress').getAttribute('aria-valuemax'), '3');
    assert.equal(submissions.length, 8);
    assert.equal(submissions[5].body.selected_index, 1);
    assert.equal(submissions[5].body.is_correct, false);
    assert.equal(submissions[5].body.revision_id, 42);
    assert.match(await page.locator('#superFinishSubtitle').innerText(), /0 очков/);
    """)


@pytest.mark.browser
def test_trivia_completed_reload_review_returns_to_initialized_summary():
    _run_browser(
        r"""
    await page.locator('#superFinishView').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#superDeltaDisplay').innerText(), '+1000');
    assert.match(await page.locator('#superFinishSubtitle').innerText(), /2500 очков/);
    await page.locator('#superReviewBtn').click();
    const trigger = page.locator('.accordion-trigger').first();
    assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
    await trigger.focus();
    await page.keyboard.press('Enter');
    assert.equal(await trigger.getAttribute('aria-expanded'), 'true');
    const bodyId = await trigger.getAttribute('aria-controls');
    assert.equal(await page.locator('#' + bodyId).isVisible(), true);
    await page.locator('#backToFinishBtn').click();
    assert.match(await page.locator('#superFinishSubtitle').innerText(), /2500 очков/);
    assert.equal(await page.locator('#superReviewBtn').evaluate(el => el === document.activeElement), true);
    """,
        completed=True,
    )


@pytest.mark.browser
def test_trivia_small_viewport_wraps_long_options_and_respects_reduced_motion():
    _run_browser(
        r"""
    await page.locator('#optionsGrid button').first().waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    for (const button of await page.locator('#optionsGrid button').all()) {
      assert.ok((await button.boundingBox()).height >= 44);
    }
    assert.equal(await page.locator('#gameView').evaluate(el => getComputedStyle(el).animationName), 'none');
    """,
        long_option=True,
    )


@pytest.mark.browser
def test_trivia_outside_telegram_does_not_claim_saved_score():
    _run_browser(
        r"""
    await page.locator('#optionsGrid button').first().waitFor();
    assert.match(await page.locator('#sessionNote').innerText(), /не сохраняется/);
    assert.equal(await page.locator('#sessionNote').isVisible(), true);
    await page.locator('#optionsGrid button').first().click();
    await page.waitForFunction(() => !document.querySelector('#nextBtn').disabled);
    assert.match(await page.locator('#answerStatus').innerText(), /вне Telegram не сохраняется/);
    """,
        telegram=False,
    )


@pytest.mark.browser
def test_trivia_load_failure_focuses_retry_and_recovers():
    _run_browser(r"""
    await page.route('**/trivia/today', route => route.fulfill({ status: 503, json: { error: 'unavailable' } }));
    await page.reload();
    await page.locator('#retryBtn').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#gameView').getAttribute('aria-busy'), 'false');
    assert.equal(await page.locator('#retryBtn').evaluate(el => el === document.activeElement), true);
    await page.unroute('**/trivia/today');
    await page.locator('#retryBtn').click();
    await page.locator('#optionsGrid button').first().waitFor();
    assert.equal(await page.locator('#retryBtn').isVisible(), false);
    assert.match(await page.locator('#qCounter').innerText(), /1\/5/);
    """)


@pytest.mark.browser
def test_trivia_loading_timeout_returns_to_retry():
    _run_browser(r"""
    await page.route('**/trivia/today', route => route.fulfill({ status: 503, json: {} }));
    await page.reload();
    await page.locator('#retryBtn').waitFor({ state: 'visible' });
    await page.clock.install();
    let releaseLoad;
    await page.unroute('**/trivia/today');
    await page.route('**/trivia/today', async route => {
      await new Promise(resolve => { releaseLoad = resolve; });
      await route.fulfill({ json: payload.data }).catch(() => {});
    });
    await page.locator('#retryBtn').click();
    await page.waitForFunction(() => document.querySelector('#gameView').getAttribute('aria-busy') === 'true');
    await page.clock.fastForward(15001);
    await page.locator('#retryBtn').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#retryBtn').isDisabled(), false);
    assert.match(await page.locator('#questionText').innerText(), /Не удалось/);
    releaseLoad();
    """)


@pytest.mark.browser
def test_trivia_save_recovery_keeps_acknowledged_answers_without_replaying():
    _run_browser(r"""
    await page.locator('#optionsGrid button').first().click();
    await page.waitForFunction(() => !document.querySelector('#nextBtn').disabled);
    await page.locator('#nextBtn').click();
    answerMode = 'fail';
    await page.locator('#optionsGrid button').nth(1).click();
    await page.locator('#saveRecoveryBtn').waitFor({ state: 'visible' });
    const beforeLoads = todayRequests;
    await page.locator('#saveRecoveryBtn').click();
    await page.locator('#daily-choice-dialog').waitFor({ state: 'visible' });
    assert.equal(submissions.length, 2, 'Recovery must not replay acknowledged answers');
    assert.equal(todayRequests, beforeLoads, 'Recovery must not restart a partial round');
    assert.equal(submissions[0].body.question_index, 0);
    assert.equal(submissions[1].body.question_index, 1);
    assert.match(await page.locator('#qCounter').innerText(), /2\/5/);
    assert.equal(await page.locator('#nextBtn').isDisabled(), true);
    assert.equal(await page.locator('#optionsGrid button:enabled').count(), 0);
    """)


@pytest.mark.browser
def test_trivia_completed_in_another_window_uses_saved_result():
    _run_browser(r"""
    await page.locator('#optionsGrid button').first().waitFor();
    payload.data.user_result = {
      status: 'completed', final_score: 1200, correct_count: 4,
      answers: [0, 1, 2, 3, 4].map(question_index => ({ question_index, selected_index: 0, is_correct: true }))
    };
    answerMode = 'already';
    await page.locator('#optionsGrid button').nth(1).click();
    await page.locator('#finishView').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#finalScore').innerText(), '1200');
    assert.match(await page.locator('#finalDetails').innerText(), /4 из 5/);
    assert.equal(submissions.length, 1);
    assert.equal(todayRequests, 2);
    """)

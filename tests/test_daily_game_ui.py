"""Offline Chromium checks for daily game navigation and Crocodile controls."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


def _run_daily_ui(scenario: str, *, game: str = "crocodile", reduced_motion: bool = False) -> None:
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    required = os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1"
    if node is None or not (browser_root / "node_modules/playwright").is_dir():
        if required:
            pytest.fail("Daily browser checks require Node.js and the locked tests/browser dependencies")
        pytest.skip("Install the locked tests/browser dependencies to run daily browser checks")
    env = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
    payload = {
        "html": env.get_template("crocodile.html").render(
            mode="daily", game_id="daily", current_daily_game=game, bot_username="test_bot"
        ),
        "switcher_js": (ROOT / "app/static/js/daily-switcher.js").read_text(encoding="utf-8"),
        "switcher_css": (ROOT / "app/static/css/daily-switcher.css").read_text(encoding="utf-8"),
        "reduced_motion": reduced_motion,
    }
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 320, height: 640 },
      reducedMotion: payload.reduced_motion ? 'reduce' : 'no-preference',
    });
    page.setDefaultTimeout(3000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.Telegram = { WebApp: {
        initData: 'offline-test', ready() {}, expand() {}, setHeaderColor() {}, setBackgroundColor() {},
        onEvent() {}, HapticFeedback: { impactOccurred() {}, notificationOccurred() {} },
      } };
      window.__sent = [];
      window.__sockets = [];
      window.WebSocket = class {
        static OPEN = 1;
        constructor(url) {
          this.url = url;
          this.readyState = 1;
          window.__sockets.push(this);
          setTimeout(() => {
            this.onopen?.({});
            this.receive({ event: 'game_state', daily: true, attempts: 0, max_attempts: 6,
              difficulty: 'easy', daily_topic: 'Животные', daily_word_mask: '_ _ _ _',
              daily_modes: [{difficulty:'easy',status:'active'}, {difficulty:'hard',status:'active'}] });
          }, 0);
        }
        send(value) { window.__sent.push(JSON.parse(value)); }
        receive(message) { this.onmessage?.({ data: JSON.stringify(message) }); }
      };
    });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'daily.test') return route.abort();
      if (url.pathname === '/static/js/daily-switcher.js')
        return route.fulfill({body:payload.switcher_js, contentType:'application/javascript'});
      if (url.pathname === '/static/css/daily-switcher.css')
        return route.fulfill({body:payload.switcher_css, contentType:'text/css'});
      if (url.pathname === '/webapp/api/daily-game') {
        const game = route.request().method() === 'PATCH' ? route.request().postDataJSON().game : '2048';
        return route.fulfill({json:{game,subscribed:false}});
      }
      return route.fulfill({body:payload.html, contentType:'text/html'});
    });
    const open = async () => {
      await page.goto('http://daily.test/webapp/game?game_id=daily');
      await page.waitForFunction(() => document.querySelector('#connecting').classList.contains('hidden'));
    };
"""
    harness += scenario
    harness += r"""
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness],
        cwd=browser_root,
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_switcher_labels_the_open_game_instead_of_the_saved_delivery_preference():
    _run_daily_ui(r"""
    await open();
    await page.waitForFunction(() => document.querySelector('[data-daily-game="2048"]').getAttribute('aria-pressed') === 'true');
    assert.match(await page.locator('#daily-choice-current').innerText(), /Крокодил/);
    """)


def test_switcher_ignores_preference_reads_that_finish_after_a_successful_save():
    _run_daily_ui(r"""
    const reads = [];
    await page.route('**/webapp/api/daily-game', route => {
      if (route.request().method() === 'GET') { reads.push(route); return; }
      return route.fulfill({json:{game:route.request().postDataJSON().game,subscribed:false}});
    });
    await open();
    await page.locator('#daily-choice-trigger').click();
    await page.waitForFunction(() => document.querySelector('#daily-choice-dialog').open);
    await page.locator('[data-daily-game="crocodile"]').click();
    await page.waitForFunction(() => !document.querySelector('#daily-choice-dialog').open);
    await Promise.all(reads.map(route => route.fulfill({json:{game:'2048',subscribed:false}})));
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('[data-daily-game="crocodile"]').getAttribute('aria-pressed'), 'true');
    """)


def test_switcher_does_not_close_when_the_player_taps_inside_the_dialog_padding():
    _run_daily_ui(r"""
    await open();
    await page.locator('#daily-choice-trigger').click();
    const box = await page.locator('#daily-choice-dialog').boundingBox();
    await page.mouse.click(box.x + 4, box.y + 4);
    assert.equal(await page.locator('#daily-choice-dialog').evaluate(element => element.open), true);
    assert.equal(await page.locator('#daily-choice-trigger').getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => document.querySelector('#daily-choice-trigger').getAttribute('aria-expanded') === 'false');
    assert.equal(await page.locator('#daily-choice-trigger').getAttribute('aria-expanded'), 'false');
    """)


def test_crocodile_enter_during_ime_composition_does_not_send_an_unfinished_guess():
    _run_daily_ui(r"""
    await open();
    await page.locator('#guess-input').fill('тигр');
    await page.locator('#guess-input').dispatchEvent('keydown', {key:'Enter',isComposing:true});
    assert.equal(await page.evaluate(() => window.__sent.filter(message => message.type === 'guess').length), 0);
    await page.locator('#guess-input').press('Enter');
    assert.equal(await page.evaluate(() => window.__sent.filter(message => message.type === 'guess').length), 1);
    assert.equal(await page.locator('#guess-input').inputValue(), '');
    """)


def test_crocodile_touch_controls_and_input_fit_a_small_phone():
    _run_daily_ui(r"""
    await open();
    assert.equal(await page.locator('#live-audio-btn').count(), 0);
    for (const selector of ['#send-btn','#hint-btn','.daily-mode-chip','#daily-choice-trigger']) {
      const box = await page.locator(selector).first().boundingBox();
      assert.ok(box.height >= 44, `${selector} needs a touch target at least 44px high`);
      assert.ok(box.x >= 0 && box.x + box.width <= 320, `${selector} must fit the viewport`);
    }
    const input = await page.locator('#guess-input').boundingBox();
    const send = await page.locator('#send-btn').boundingBox();
    assert.ok(input.x + input.width <= send.x, 'the input cannot push the send button off screen');
    for (const height of [400,350,300]) {
      await page.setViewportSize({width:320,height});
      await page.waitForFunction(() => document.querySelector('#app').getBoundingClientRect().bottom <= window.innerHeight + 1);
      const shortSend = await page.locator('#send-btn').boundingBox();
      assert.ok(shortSend.y + shortSend.height <= height, `the composer must remain visible with ${height}px above the keyboard`);
    }
    """)


def test_crocodile_mode_progress_uses_real_attempts_without_inventing_other_mode_counts():
    _run_daily_ui(r"""
    await open();
    await page.evaluate(() => window.__sockets[0].receive({event:'game_state',daily:true,
      attempts:4,max_attempts:6,difficulty:'easy',daily_modes:[
        {difficulty:'easy',status:'active',completed:false},
        {difficulty:'hard',status:'active',completed:false}]}));
    assert.match(await page.locator('[data-difficulty="easy"]').innerText(), /2/);
    assert.doesNotMatch(await page.locator('[data-difficulty="hard"]').innerText(), /[0-9]/);
    await page.evaluate(() => window.__sockets[0].receive({event:'result',status:'cold',attempts:5}));
    assert.match(await page.locator('[data-difficulty="easy"]').innerText(), /1/);
    """)


def test_crocodile_results_explain_temperature_without_relying_on_color():
    _run_daily_ui(r"""
    await open();
    await page.locator('#guess-input').fill('тигр');
    await page.locator('#send-btn').click();
    await page.evaluate(() => {
      const guess = window.__sent.find(message => message.type === 'guess');
      window.__sockets[0].receive({event:'result',pending_id:guess.pending_id,status:'warm',score:.55,attempts:1,hint:'Ближе!'});
    });
    const bubble = page.locator('.bubble.user.warm');
    assert.match(await bubble.innerText(), /тепло/i);
    assert.equal(await page.locator('#typing-row').evaluate(element => element.classList.contains('hidden')), true);
    """)


def test_crocodile_completion_can_be_closed_to_read_attempts_and_choose_another_game():
    _run_daily_ui(r"""
    await open();
    await page.evaluate(() => window.__sockets[0].receive({event:'daily_completed',won:true,word:'тигр',attempts:2,points:880}));
    assert.equal(await page.locator('#overlay').getAttribute('aria-modal'), 'true');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#overlay').evaluate(element => element.classList.contains('visible')), false);
    assert.equal(await page.locator('#guess-input').isDisabled(), true);
    await page.locator('#daily-choice-trigger').click();
    assert.equal(await page.locator('#daily-choice-dialog').evaluate(element => element.open), true);
    """)


def test_crocodile_a_delayed_win_animation_does_not_reopen_a_dismissed_result():
    _run_daily_ui(r"""
    await page.clock.install();
    await open();
    await page.evaluate(() => {
      window.__sockets[0].receive({event:'result',status:'exact_match',word:'тигр',attempts:2});
      window.__sockets[0].receive({event:'daily_completed',won:true,word:'тигр',attempts:2,points:880});
    });
    await page.keyboard.press('Escape');
    await page.clock.runFor(650);
    assert.equal(await page.locator('#overlay').evaluate(element => element.classList.contains('visible')), false);
    """)


def test_crocodile_respects_reduced_motion_for_pending_guesses_and_typing():
    _run_daily_ui(
        r"""
    await open();
    await page.locator('#guess-input').fill('тигр');
    await page.locator('#send-btn').click();
    const durations = await page.locator('.bubble.pending, .typing-dots span').evaluateAll(elements =>
      elements.flatMap(element => getComputedStyle(element).animationDuration.split(',').map(parseFloat)));
    assert.ok(durations.every(duration => duration <= .001), 'decorative loops must stop with reduced motion');
    """,
        reduced_motion=True,
    )

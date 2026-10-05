"""Offline browser contracts for tile movement and server-confirmed input."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _run_browser(scenario: str, *, reduced_motion: bool = False, record_video: str | None = None) -> None:
    root = Path(__file__).resolve().parents[1]
    browser_root = root / "tests/browser"
    node = shutil.which("node")
    required = os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1"
    if node is None:
        if required:
            pytest.fail("Required browser regression needs Node.js")
        pytest.skip("Optional browser regression needs Node.js")
    probe = subprocess.run(
        [node, "-e", "require.resolve('playwright')"],
        cwd=browser_root,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )
    if probe.returncode:
        if required:
            pytest.fail("Required browser regression needs tests/browser npm ci")
        pytest.skip("Optional browser regression needs Playwright")
    html = (root / "app/templates/daily_2048.html").read_text(encoding="utf-8")
    switcher = (root / "app/templates/_daily_game_switcher.html").read_text(encoding="utf-8")
    html = html.replace("{% include '_daily_game_switcher.html' %}", switcher)
    payload = {
        "html": html,
        "reduced_motion": reduced_motion,
        "switcher_css": (root / "app/static/css/daily-switcher.css").read_text(encoding="utf-8"),
        "record_video": record_video,
    }
    harness = r"""
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const payload = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 640 },
      reducedMotion: payload.reduced_motion ? 'reduce' : 'no-preference',
      recordVideo: payload.record_video ? { dir:require('node:path').dirname(payload.record_video),
        size:{width:390,height:844} } : undefined });
    page.setDefaultTimeout(4000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.hostname === 'daily.test' && url.pathname === '/2048') {
        return route.fulfill({ body: payload.html, contentType: 'text/html' });
      }
      if (url.pathname === '/static/css/daily-switcher.css') {
        return route.fulfill({ body: payload.switcher_css, contentType: 'text/css' });
      }
      return route.fulfill({ body: '', contentType: url.pathname.endsWith('.css') ? 'text/css' : 'application/javascript' });
    });
    await page.addInitScript(() => {
      window.sockets = [];
      class Socket {
        static OPEN = 1;
        readyState = 1;
        sent = [];
        constructor() { window.sockets.push(this); }
        send(data) { this.sent.push(JSON.parse(data)); }
        receive(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
        close() { this.readyState = 3; this.onclose?.({ code: 1006 }); }
      }
      window.WebSocket = Socket;
    });
    await page.goto('http://daily.test/2048');
    const initial = {
      event: 'game_state', seq: 1, status: 'active', recordable: true,
      board: [[2,2,4,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]],
      start_board: [[2,2,4,0],[0,0,0,0],[0,0,0,0],[0,0,0,0]],
      moves: 0, merge_score: 0, final_score: 0, elapsed_ms: 0,
      goal: { type: 'tile', value: 2048, label: 'Собери кубик 2048', help: 'Слей одинаковые кубики.' }
    };
    await page.evaluate(state => window.sockets[0].receive(state), initial);
    const moves = () => page.evaluate(() => window.sockets.at(-1).sent.filter(item => item.type === 'move'));
    const ack = async (index, board, extra = {}) => {
      const sent = await moves();
      await page.evaluate(message => window.sockets.at(-1).receive(message), {
        event: 'move_result', seq: index + 2, pending_id: sent[index].pending_id,
        board, moved: true, status: 'active', recordable: true,
        moves: index + 1, merge_score: 4, final_score: 0, elapsed_ms: 100, ...extra
      });
    };
"""
    harness += scenario
    harness += r"""
    assert.deepEqual(errors, []);
    if (payload.record_video) {
      const video = page.video();
      await page.close();
      await video.saveAs(payload.record_video);
      require('node:fs').unlinkSync(await video.path());
    }
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
def test_tiles_keep_identity_and_both_merge_sources_slide_before_value_changes():
    _run_browser(
        r"""
    await page.evaluate(() => { window.originalTiles = [...document.querySelectorAll('#tiles .tile')]; });
    await page.keyboard.press('ArrowLeft');
    assert.equal(await page.evaluate(() => window.originalTiles.every(tile => tile.isConnected)), true,
      'a slide must move the existing tiles instead of replacing the board');
    assert.deepEqual(await page.evaluate(() => window.originalTiles.map(tile => tile.textContent)), ['2','2','4'],
      'merge values must change only after the two sources meet');
    await page.waitForTimeout(60);
    const mid = await page.evaluate(() => window.originalTiles.map(tile => tile.getBoundingClientRect().x));
    assert.ok(mid[1] > mid[0] && mid[2] > mid[1], 'both source tiles must be visible during sliding');
    await page.evaluate(async () => {
      const animations = [...document.querySelectorAll('#tiles .tile')]
        .flatMap(tile => tile.getAnimations());
      await Promise.all(animations.map(animation => animation.finished));
    });
    await page.waitForFunction(() => document.querySelectorAll('#tiles .tile').length === 2);
    assert.equal(await page.evaluate(() => window.originalTiles[2].isConnected), true,
      'the existing 4 must remain the second 4 rather than being misassigned to the merge');
    assert.deepEqual(await page.locator('#tiles .tile').allTextContents(), ['4','4']);
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      { spawned: { x: 3, y: 3, value: 2 } });
    assert.equal(await page.evaluate(() => window.originalTiles[2].isConnected), true,
      'server acknowledgement must preserve tile identity');
    assert.equal(await page.locator('#tiles .tile').count(), 3);
"""
    )


@pytest.mark.browser
def test_spawn_stays_centred_in_its_cell_during_appearance():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(240);
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      { spawned: { x:3,y:3,value:2 } });
    const offsets = await page.evaluate(async () => {
      const tile = document.querySelector('#tiles .tile.spawn');
      const cell = document.querySelectorAll('.grid .cell')[15].getBoundingClientRect();
      const offsets = [];
      for (let frame = 0; frame < 8; frame++) {
        await new Promise(requestAnimationFrame);
        const glyph = tile.querySelector('span').getBoundingClientRect();
        offsets.push(Math.hypot(glyph.x + glyph.width/2 - cell.x - cell.width/2,
          glyph.y + glyph.height/2 - cell.y - cell.height/2));
      }
      return offsets;
    });
    assert.ok(offsets.every(offset => offset < 1),
      `appearing in the bottom-right cell must not pull the tile towards the board origin: ${offsets}`);
"""
    )


@pytest.mark.browser
def test_merge_feedback_stays_centred_at_the_board_edge():
    _run_browser(
        r"""
    await page.evaluate(state => window.sockets[0].receive(state), {
      ...initial, seq:2, board:[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,2,2,0]]
    });
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(() => document.querySelector('#tiles .tile')?.textContent === '4');
    const offsets = await page.evaluate(async () => {
      const tile = document.querySelector('#tiles .tile');
      const cell = document.querySelectorAll('.grid .cell')[15].getBoundingClientRect();
      const offsets = [];
      for (let frame = 0; frame < 8; frame++) {
        await new Promise(requestAnimationFrame);
        const glyph = tile.querySelector('span').getBoundingClientRect();
        offsets.push(Math.hypot(glyph.x + glyph.width/2 - cell.x - cell.width/2,
          glyph.y + glyph.height/2 - cell.y - cell.height/2));
      }
      return offsets;
    });
    assert.ok(offsets.every(offset => offset < 1),
      `merge emphasis must stay centred at the destination: ${offsets}`);
"""
    )


@pytest.mark.browser
def test_server_spawn_can_appear_while_a_queued_move_starts():
    _run_browser(
        r"""
    await page.evaluate(() => {
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node.nodeType === 1 && node.matches('.tile') && node.textContent === '2') window.newSpawn = node;
        }
      }).observe(document.querySelector('#tiles'), { childList:true });
    });
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      { spawned: { x:3,y:3,value:2 } });
    await page.waitForFunction(() => window.newSpawn?.isConnected);
    const opacities = await page.evaluate(async () => {
      const opacities = [];
      for (let frame = 0; frame < 8; frame++) {
        await new Promise(requestAnimationFrame);
        opacities.push(Number(getComputedStyle(window.newSpawn).opacity) *
          Number(getComputedStyle(window.newSpawn.firstElementChild).opacity));
      }
      return opacities;
    });
    assert.ok(opacities.some(opacity => opacity > 0 && opacity < .98),
      `the following slide must not cut off spawn appearance before the first painted frame: ${opacities}`);
"""
    )


@pytest.mark.browser
def test_fast_input_waits_for_ack_and_stale_ack_cannot_release_the_next_move():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('ArrowRight');
    assert.equal((await moves()).length, 1, 'only one move may be in flight');
    const first = (await moves())[0];
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      { spawned: { x: 3, y: 3, value: 2 } });
    await page.waitForFunction(() => window.sockets[0].sent.filter(item => item.type === 'move').length === 2);
    const second = (await moves())[1];
    assert.equal(second.direction, 'down', 'fast input must be retained in order');
    assert.deepEqual(second.client_board_before, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      'queued input starts from the server-confirmed spawned board');
    await page.evaluate(message => window.sockets[0].receive(message), {
      event: 'move_result', seq: 2, pending_id: first.pending_id,
      board: first.client_board_after, status: 'active', moves: 1, recordable: true
    });
    await page.waitForTimeout(220);
    assert.equal((await moves()).length, 2, 'a duplicate ack must not release another queued move');
    await ack(1, [[0,0,0,2],[0,0,0,0],[0,0,0,0],[4,4,0,2]],
      { spawned: { x: 3, y: 0, value: 2 } });
    await page.waitForFunction(() => window.sockets[0].sent.filter(item => item.type === 'move').length === 3);
    assert.equal((await moves())[2].direction, 'right');
    assert.equal(new Set((await moves()).map(item => item.pending_id)).size, 3);
"""
    )


@pytest.mark.browser
def test_keyboard_does_not_capture_editable_fields_or_open_dialogs():
    _run_browser(
        r"""
    await page.evaluate(() => {
      const input = document.createElement('input'); input.id = 'test-input'; document.body.append(input); input.focus();
    });
    await page.keyboard.press('ArrowLeft');
    assert.equal((await moves()).length, 0, 'cursor keys in forms must not move the board');
    await page.evaluate(() => {
      document.getElementById('test-input').blur();
      const dialog = document.createElement('dialog'); dialog.id = 'test-dialog';
      dialog.innerHTML = '<button>OK</button>'; document.body.append(dialog); dialog.showModal();
    });
    await page.keyboard.press('ArrowDown');
    assert.equal((await moves()).length, 0, 'all modal dialogs must own their keyboard input');
"""
    )


@pytest.mark.browser
def test_reconnect_resumes_authoritative_board_without_replaying_pending_or_queued_input():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await page.evaluate(() => window.sockets[0].close());
    await page.waitForFunction(() => window.sockets.length === 2);
    await page.evaluate(state => window.sockets[1].receive(state), {
      ...initial, seq: 20, board: [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]], moves: 1
    });
    await page.waitForTimeout(220);
    assert.equal((await moves()).length, 0, 'uncertain moves must never be replayed after reconnect');
    await page.evaluate(state => window.sockets[0].receive(state), initial);
    assert.equal(await page.locator('#moves').textContent(), '1', 'events from an old socket must be ignored');
    await page.keyboard.press('ArrowDown');
    assert.deepEqual((await moves())[0].client_board_before, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]]);
"""
    )


@pytest.mark.browser
def test_rejected_optimistic_move_drops_queued_input_and_restores_server_board():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await ack(0, initial.board, { moved: false, moves: 0, merge_score: 0 });
    await page.waitForTimeout(200);
    assert.equal((await moves()).length, 1, 'queued intentions must be dropped after a correction');
    assert.deepEqual(await page.locator('#tiles .tile').allTextContents(), ['2','2','4']);
    await page.keyboard.press('ArrowRight');
    assert.deepEqual((await moves())[1].client_board_before, initial.board);
"""
    )


@pytest.mark.browser
def test_network_error_restores_snapshot_and_discards_fast_input():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await page.evaluate(() => window.sockets[0].receive({ event: 'error', seq: 2, message: 'Busy' }));
    await page.waitForTimeout(180);
    assert.deepEqual(await page.locator('#tiles .tile').allTextContents(), ['2','2','4']);
    assert.equal((await moves()).length, 1);
    await page.keyboard.press('ArrowRight');
    assert.deepEqual((await moves())[1].client_board_before, initial.board);
"""
    )


@pytest.mark.browser
def test_input_queue_is_bounded_and_a_cancelled_pointer_never_moves():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    for (const direction of ['ArrowDown','ArrowRight','ArrowUp','ArrowLeft']) await page.keyboard.press(direction);
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]], { spawned: { x: 3, y: 3, value: 2 } });
    assert.equal((await moves())[1].direction, 'down');
    await ack(1, [[0,0,0,2],[0,0,0,0],[0,0,0,0],[4,4,0,2]], { spawned: { x: 3, y: 0, value: 2 } });
    assert.equal((await moves())[2].direction, 'right');
    await ack(2, [[0,0,0,2],[0,0,0,0],[0,0,0,0],[0,0,8,2]]);
    await page.waitForTimeout(450);
    assert.equal((await moves()).length, 3, 'only two pending directions may accumulate');
    assert.deepEqual((await page.locator('#tiles .tile').allTextContents()).sort(), ['2','2','8'],
      'the visual pipeline must catch up even when acknowledgements arrive during earlier slides');
    const bounds = await page.locator('#board').boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(bounds.x + 20, bounds.y + bounds.height / 2);
    await page.locator('#board').dispatchEvent('pointercancel');
    await page.mouse.up();
    assert.equal((await moves()).length, 3, 'cancelled gestures must not be submitted');
"""
    )


@pytest.mark.browser
def test_finish_waits_for_the_winning_merge_before_covering_the_board():
    _run_browser(
        r"""
    await page.evaluate(state => window.sockets[0].receive(state), {
      ...initial, seq:2, board:[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,1024,1024,0]]
    });
    await page.keyboard.press('ArrowRight');
    await ack(0, [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,2,2048]], {
      seq:3, spawned:{x:2,y:3,value:2}, status:'won', daily2048_completed:true,
      game_over:true, gained_score:2048, merge_score:2048
    });
    assert.equal(await page.locator('#overlay').isVisible(), false,
      'the result must not cover the winning tiles before they meet');
    await page.waitForFunction(() => [...document.querySelectorAll('#tiles .tile')].some(tile => tile.textContent === '2048'));
    assert.equal(await page.locator('#overlay').isVisible(), false,
      'the merged target needs a short visible emphasis before the result');
    await page.waitForFunction(() => document.querySelector('#overlay').classList.contains('visible'));
    assert.equal(await page.locator('#tiles').evaluate(tiles => tiles.getAnimations({subtree:true}).some(
      animation => animation.playState === 'running')), false, 'the result follows actual completion of tile feedback');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'practice-btn');
    assert.equal((await moves()).length, 1);
"""
    )


@pytest.mark.browser
def test_a_new_snapshot_cancels_a_result_waiting_for_visual_completion():
    _run_browser(
        r"""
    await page.evaluate(state => window.sockets[0].receive(state), {
      ...initial, seq:2, board:[[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,1024,1024,0]]
    });
    await page.keyboard.press('ArrowRight');
    await ack(0, [[0,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,2,2048]], {
      seq:3, spawned:{x:2,y:3,value:2}, status:'won', daily2048_completed:true, game_over:true
    });
    await page.waitForFunction(() => [...document.querySelectorAll('#tiles .tile')].some(tile => tile.textContent === '2048'));
    const resumed = {...initial, seq:4, moves:7, board:[[32,0,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,16]]};
    await page.evaluate(state => window.sockets[0].receive(state), resumed);
    await page.waitForTimeout(320);
    assert.equal(await page.locator('#overlay').isVisible(), false, 'an old result must not cover the fresh snapshot');
    assert.equal(await page.locator('#app').evaluate(element => element.inert), false);
    assert.deepEqual(await page.locator('#tiles .tile').allTextContents(), ['32','16']);
    await page.keyboard.press('ArrowRight');
    assert.deepEqual((await moves())[1].client_board_before, resumed.board);
"""
    )


@pytest.mark.browser
@pytest.mark.parametrize("terminal", ["won", "lost"])
def test_terminal_daily_result_can_continue_or_restart_practice(terminal):
    _run_browser(
        r"""
    const terminal = """
        + json.dumps(terminal)
        + r""";
    if (terminal === 'lost') {
      await page.evaluate(state => window.sockets[0].receive(state), {
        ...initial, seq: 2, status: 'lost', board: [[2,4,2,4],[4,2,4,2],[2,4,2,4],[4,2,4,2]]
      });
    } else {
      await page.keyboard.press('ArrowLeft');
      await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]], {
        spawned: { x: 3, y: 3, value: 2 }, status: 'won', daily2048_completed: true, game_over: true
      });
    }
    await page.waitForFunction(() => document.querySelector('#overlay').classList.contains('visible'));
    assert.equal(await page.locator('#overlay').isVisible(), true);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'practice-btn');
    await page.locator('#practice-btn').click();
    await page.keyboard.press(terminal === 'lost' ? 'ArrowLeft' : 'ArrowDown');
    const submitted = (await moves()).at(-1);
    assert.deepEqual(submitted.client_board_before, terminal === 'lost' ? initial.start_board :
      [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]]);
    await page.evaluate(message => window.sockets[0].receive(message), {
      event: 'move_result', seq: 3, pending_id: submitted.pending_id, board: submitted.client_board_after,
      moved: true, status: 'active', recordable: false, moves: 1, merge_score: 4, elapsed_ms: 200
    });
    assert.equal(await page.locator('#overlay').isVisible(), false);
    assert.equal(await page.locator('#record-state').textContent(), 'PLAY');
""",
        reduced_motion=True,
    )


@pytest.mark.browser
def test_reduced_motion_has_instant_merges_and_controls_fit_small_viewports():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    assert.deepEqual(await page.locator('#tiles .tile').allTextContents(), ['4','4']);
    assert.equal(await page.locator('#tiles .tile').first().evaluate(element => getComputedStyle(element).animationName), 'none');
    for (const theme of ['aero','obsidian','material']) {
    await page.evaluate(theme => applyTheme(theme), theme);
    for (const viewport of [{width:320,height:640},{width:320,height:480},{width:390,height:844},{width:900,height:600}]) {
      await page.setViewportSize(viewport);
      const geometry = await page.evaluate(() => {
        const board = document.getElementById('board').getBoundingClientRect();
        const controls = [...document.querySelectorAll('.controls button')].map(element => element.getBoundingClientRect());
        return { width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
          board: { x: board.x, right: board.right, width: board.width, height: board.height },
          controls: controls.map(box => ({width:box.width,height:box.height})) };
      });
      assert.ok(geometry.scrollWidth <= geometry.width, 'a 320px viewport must not overflow');
      assert.ok(geometry.board.x >= 0 && geometry.board.right <= geometry.width);
      assert.ok(Math.abs(geometry.board.width - geometry.board.height) < 1);
      assert.ok(geometry.controls.every(box => box.width >= 44 && box.height >= 44));
    }
    }
""",
        reduced_motion=True,
    )


@pytest.mark.browser
@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("Left", [[4, 4, 0, 0], [8, 0, 0, 0], [4, 4, 0, 0], [0, 0, 0, 0]]),
        ("Right", [[0, 0, 4, 4], [0, 0, 0, 8], [0, 0, 4, 4], [0, 0, 0, 0]]),
        ("Up", [[2, 4, 8, 2], [4, 0, 2, 0], [2, 0, 0, 0], [0, 0, 0, 0]]),
        ("Down", [[0, 0, 0, 0], [2, 0, 0, 0], [4, 0, 8, 0], [2, 4, 2, 2]]),
    ],
)
def test_directional_slide_places_each_merge_in_the_correct_cell(direction, expected):
    _run_browser(
        r"""
    const fixture = """
        + json.dumps({"direction": direction, "expected": expected})
        + r""";
    await page.evaluate(state => window.sockets[0].receive(state), {
      ...initial, seq: 2, board: [[2,2,4,0],[4,0,4,0],[2,2,2,2],[0,0,0,0]]
    });
    await page.keyboard.press(`Arrow${fixture.direction}`);
    assert.deepEqual((await moves())[0].client_board_after, fixture.expected,
      'the proposed board must merge each original tile at most once');
    await page.waitForTimeout(320);
    const visibleBoard = await page.evaluate(() => {
      const board = Array.from({length:4}, () => [0,0,0,0]);
      const cells = [...document.querySelectorAll('.grid .cell')].map(element => element.getBoundingClientRect());
      for (const tile of document.querySelectorAll('#tiles .tile')) {
        const box = tile.getBoundingClientRect();
        const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
        const index = cells.findIndex(cell => cx >= cell.x && cx <= cell.right && cy >= cell.y && cy <= cell.bottom);
        if (index >= 0) board[Math.floor(index / 4)][index % 4] = Number(tile.textContent);
      }
      return board;
    });
    assert.deepEqual(visibleBoard, fixture.expected, 'tile geometry and values must agree with the proposed board');
"""
    )


@pytest.mark.browser
def test_merge_feedback_survives_the_completion_of_a_fast_following_slide():
    _run_browser(
        r"""
    await page.evaluate(() => { window.mergeSurvivor = document.querySelector('#tiles .tile'); });
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]], { spawned: { x: 3, y: 3, value: 2 } });
    await page.waitForFunction(() => window.mergeSurvivor.textContent === '4');
    await page.waitForFunction(() => window.mergeSurvivor.getAnimations().some(animation => animation.playState === 'running'));
    await page.waitForFunction(() => window.mergeSurvivor.getAnimations().every(animation => animation.playState !== 'running'));
    assert.equal(await page.evaluate(() => window.mergeSurvivor.firstElementChild.getAnimations().some(
      animation => animation.playState === 'running')), true,
      'finishing the queued slide must preserve independent merge feedback on the same tile');
"""
    )


@pytest.mark.browser
def test_unanswered_move_recovers_connection_without_resending_it():
    _run_browser(
        r"""
    await page.clock.install();
    await page.keyboard.press('ArrowLeft');
    await page.keyboard.press('ArrowDown');
    await page.clock.runFor(17000);
    assert.equal(await page.evaluate(() => window.sockets.length), 2,
      'an unanswered move must recover instead of locking all controls forever');
    assert.equal((await moves()).length, 0, 'timeout recovery must never resend an uncertain move');
"""
    )


@pytest.mark.browser
def test_fast_confirmed_moves_have_bounded_visible_catchup_then_resume_normal_sliding():
    from app.games.daily_2048 import apply_move

    # The real server move reducer supplies independent authoritative fixtures.
    current = [[2, 2, 4, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
    fixtures = []
    for index in range(24):
        for offset in range(4):
            direction = ["right", "down", "left", "up"][(index + offset) % 4]
            outcome = apply_move(current, direction)
            if outcome.moved:
                break
        assert outcome.moved
        next_board = [row[:] for row in outcome.board]
        x, y = next((x, y) for y in range(4) for x in range(4) if not next_board[y][x])
        next_board[y][x] = 2
        fixtures.append({"direction": direction, "board": next_board, "spawned": {"x": x, "y": y, "value": 2}})
        current = next_board
    _run_browser(
        r"""
    const fixtures = """
        + json.dumps(fixtures)
        + r""";
    await page.evaluate(fixtures => {
      const board = document.getElementById('board');
      fixtures.forEach((fixture, index) => {
        board.dispatchEvent(new KeyboardEvent('keydown', { key: `Arrow${fixture.direction[0].toUpperCase()}${fixture.direction.slice(1)}`, bubbles: true }));
        const move = window.sockets[0].sent.filter(message => message.type === 'move').at(-1);
        window.sockets[0].receive({ event:'move_result', seq:index+2, pending_id:move.pending_id,
          board:fixture.board, spawned:fixture.spawned, moved:true, status:'active', recordable:true,
          moves:index+1, merge_score:100, elapsed_ms:100 });
      });
    }, fixtures);
    await page.waitForTimeout(450);
    const visibleBoard = await page.evaluate(() => {
      const board = Array.from({length:4}, () => [0,0,0,0]);
      const cells = [...document.querySelectorAll('.grid .cell')].map(element => element.getBoundingClientRect());
      for (const tile of document.querySelectorAll('#tiles .tile')) {
        const box = tile.getBoundingClientRect(), cx = box.x + box.width/2, cy = box.y + box.height/2;
        const index = cells.findIndex(cell => cx >= cell.x && cx <= cell.right && cy >= cell.y && cy <= cell.bottom);
        if (index >= 0) board[Math.floor(index/4)][index%4] = Number(tile.textContent);
      }
      return board;
    });
    assert.equal((await moves()).length, 24, 'network dispatch must stay immediate during catchup');
    assert.deepEqual(visibleBoard, fixtures.at(-1).board, 'visible tiles must catch up within a bounded animation window');
    await page.evaluate(() => { window.afterCatchupTiles = [...document.querySelectorAll('#tiles .tile')]; });
    for (const key of ['ArrowLeft','ArrowRight','ArrowUp','ArrowDown']) {
      await page.keyboard.press(key);
      if ((await moves()).length > 24) break;
    }
    assert.equal(await page.evaluate(() => window.afterCatchupTiles.every(tile => tile.isConnected)), true);
    assert.equal(await page.locator('#tiles .tile').evaluateAll(tiles => tiles.some(
      tile => tile.getAnimations().some(animation => animation.playState === 'running'))), true,
      'ordinary input must still animate after the burst');
"""
    )


@pytest.mark.browser
def test_paced_fast_moves_keep_visible_intermediate_positions_without_teleports():
    from app.games.daily_2048 import apply_move

    current = [[2, 2, 4, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
    fixtures = []
    for index in range(24):
        for offset in range(4):
            direction = ["right", "down", "left", "up"][(index + offset) % 4]
            outcome = apply_move(current, direction)
            if outcome.moved:
                break
        assert outcome.moved
        current = [row[:] for row in outcome.board]
        x, y = next((x, y) for y in range(4) for x in range(4) if not current[y][x])
        current[y][x] = 2
        fixtures.append({"direction": direction, "board": current, "spawned": {"x": x, "y": y, "value": 2}})
    _run_browser(
        r"""
    const fixtures = """
        + json.dumps(fixtures)
        + r""";
    const motion = await page.evaluate(async fixtures => {
      const previous = new WeakMap();
      const cells = [...document.querySelectorAll('.grid .cell')].map(cell => cell.getBoundingClientRect());
      const pitch = cells[1].x - cells[0].x;
      let maxStep = 0, movingFrames = 0, done = false;
      const sample = time => {
        for (const tile of document.querySelectorAll('#tiles .tile')) {
          const box = tile.querySelector('span').getBoundingClientRect();
          const position = {x:box.x+box.width/2, y:box.y+box.height/2, time};
          const before = previous.get(tile);
          if (before && time-before.time <= 25) {
            const step = Math.hypot(position.x-before.x, position.y-before.y) / pitch;
            maxStep = Math.max(maxStep, step);
            if (step > .02 && step < 2) movingFrames++;
          }
          previous.set(tile, position);
        }
        if (!done) requestAnimationFrame(sample);
      };
      requestAnimationFrame(sample);
      for (const [index, fixture] of fixtures.entries()) {
        document.getElementById('board').dispatchEvent(new KeyboardEvent('keydown', {
          key:`Arrow${fixture.direction[0].toUpperCase()}${fixture.direction.slice(1)}`, bubbles:true
        }));
        const move = window.sockets[0].sent.filter(message => message.type === 'move').at(-1);
        setTimeout(() => window.sockets[0].receive({event:'move_result', seq:index+2,
          pending_id:move.pending_id, board:fixture.board, spawned:fixture.spawned,
          moved:true, status:'active', recordable:true, moves:index+1, merge_score:100, elapsed_ms:100}), 50);
        await new Promise(resolve => setTimeout(resolve, 70));
      }
      await new Promise(resolve => setTimeout(resolve, 350));
      done = true;
      const visible = Array.from({length:4}, () => [0,0,0,0]);
      for (const tile of document.querySelectorAll('#tiles .tile')) {
        const box = tile.querySelector('span').getBoundingClientRect();
        const cx=box.x+box.width/2, cy=box.y+box.height/2;
        const index = cells.findIndex(cell => cx >= cell.x && cx <= cell.right && cy >= cell.y && cy <= cell.bottom);
        if (index >= 0) visible[Math.floor(index/4)][index%4] = Number(tile.textContent);
      }
      return {maxStep, movingFrames, visible};
    }, fixtures);
    assert.ok(motion.movingFrames > 24, 'paced fast play needs multiple intermediate painted positions');
    assert.ok(motion.maxStep < 2.1, `ordinary fast input must not teleport across the board: ${motion.maxStep} cells/frame`);
    assert.deepEqual(motion.visible, fixtures.at(-1).board);
    assert.equal((await moves()).length, 24);
"""
    )


@pytest.mark.browser
def test_three_distinct_themes_cycle_and_persist_accessible_selection():
    _run_browser(
        r"""
    assert.equal(await page.locator('#theme-label').textContent(), 'Aero');
    await page.locator('#theme-btn').click();
    assert.equal(await page.locator('#theme-label').textContent(), 'Obsidian');
    const obsidian = await page.locator('body').evaluate(element => getComputedStyle(element).color);
    await page.locator('#theme-btn').click();
    assert.equal(await page.locator('#theme-label').textContent(), 'Material');
    const material = await page.locator('body').evaluate(element => getComputedStyle(element).color);
    assert.notEqual(material, obsidian, 'theme choice must change the material and contrast, not just a label');
    assert.ok((await page.locator('#theme-btn').getAttribute('aria-label')).includes('Material'));
    await page.reload();
    assert.equal(await page.locator('#theme-label').textContent(), 'Material');
    await page.locator('#theme-btn').click();
    assert.equal(await page.locator('#theme-label').textContent(), 'Aero');
"""
    )


@pytest.mark.browser
@pytest.mark.parametrize(
    ("legacy", "current"),
    [("desk", "Obsidian"), ("botanical", "Obsidian"), ("swiss", "Material"), ("deco", "Material")],
)
def test_saved_legacy_theme_selection_migrates_to_current_materials(legacy, current):
    _run_browser(
        r"""
    const fixture = """
        + json.dumps({"legacy": legacy, "current": current})
        + r""";
    await page.evaluate(legacy => localStorage.setItem('daily2048:theme', legacy), fixture.legacy);
    await page.reload();
    assert.equal(await page.locator('#theme-label').textContent(), fixture.current);
    assert.equal(await page.evaluate(() => localStorage.getItem('daily2048:theme')), fixture.current.toLowerCase());
"""
    )


@pytest.mark.browser
def test_confirmed_merge_shows_score_gain_without_double_counting_duplicate_ack():
    _run_browser(
        r"""
    await page.keyboard.press('ArrowLeft');
    await ack(0, [[4,4,0,0],[0,0,0,0],[0,0,0,0],[0,0,0,2]],
      { spawned: { x:3,y:3,value:2 }, gained_score:4 });
    assert.equal(await page.locator('#score').textContent(), '4');
    await page.waitForFunction(() => document.querySelector('#score-gain').textContent === '+4');
    assert.equal(await page.locator('#score-gain').textContent(), '+4');
    const accepted = (await moves())[0];
    await page.evaluate(message => window.sockets[0].receive(message), {
      event:'move_result', seq:2, pending_id:accepted.pending_id, board:accepted.client_board_after,
      moves:1, merge_score:4, gained_score:4, status:'active', recordable:true
    });
    assert.equal(await page.locator('#score').textContent(), '4');
"""
    )

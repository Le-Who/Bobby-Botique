"""Execute the Mini App recovery path: retain a failed guess and the current draft."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_failed_guess_can_be_retried_without_retyping_or_overwriting_draft():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for Mini App JavaScript tests")
    template = (Path(__file__).parents[1] / "app/templates/crocodile.html").read_text(encoding="utf-8")
    helpers = template[
        template.index("function removePendingBubble") : template.index("function appendCompletedBubble")
    ]
    failure_case = template[template.index("    case 'judge_unavailable':") : template.index("    case 'game_over':")]
    send_guess = template[template.index("function sendGuess(") : template.index("$('send-btn').addEventListener")]
    script = (
        """
const assert = require('node:assert/strict');
let row, bubble, meta, input, sent, pending, ws;
let gameOver = false;
const isCreatorMode = false;
const WebSocket = {OPEN: 1};
const haptic = null;
const showError = () => {};
const updateTyping = () => {};
const uid = () => 'new-pending-id';
const $ = () => input;
const appendPendingBubble = (word, pid) => pending.push({word, pid});
const document = {
  getElementById: id => id === 'bubble-failed-id' && !row.removed ? row : null,
  createElement: tag => ({
    tagName: tag.toUpperCase(),
    addEventListener(type, callback) { this[type] = callback; },
  }),
};
"""
        + helpers
        + send_guess
        + "\nfunction receive(msg) { switch (msg.event) {\n"
        + failure_case
        + "\n} }\n"
        + """
for (const draft of ['', 'новая догадка']) {
  for (const state of ['connected', 'disconnected', 'finished']) {
    sent = [];
    pending = [];
    gameOver = state === 'finished';
    input = {value: draft, focus() {}};
    ws = {readyState: state === 'disconnected' ? 3 : 1,
          send: value => sent.push(JSON.parse(value))};
    meta = {children: [], replaceChildren(...items) { this.children = items; }};
    bubble = {className: 'bubble user pending', querySelector: () => meta};
    bubble.classList = {remove(name) {
      bubble.className = bubble.className.split(' ').filter(item => item !== name).join(' ');
    }};
    row = {removed: false,
           querySelector: selector => selector === '.b-word' ? {textContent:'тигр'} : bubble,
           remove() { this.removed = true; }};

    receive({event:'judge_unavailable', pending_id:'failed-id', message:'Попытка не засчитана'});
    assert.equal(row.removed, false, 'the failed word must remain available');
    assert.equal(input.value, draft, 'failure must preserve the next draft');
    assert.equal(bubble.className.includes('pending'), false, 'failure must stop the spinner');
    const retry = meta.children.find(element => element.tagName === 'BUTTON');
    assert.ok(retry, 'the player needs a retry action beside the failed word');
    retry.click();
    const guesses = sent.filter(message => message.type === 'guess');
    if (state === 'connected') {
      assert.deepEqual(guesses, [{type:'guess', word:'тигр', pending_id:'new-pending-id'}]);
      assert.deepEqual(pending, [{word:'тигр', pid:'new-pending-id'}]);
      assert.equal(row.removed, true);
    } else {
      assert.deepEqual(guesses, []);
      assert.equal(row.removed, false);
    }
    assert.equal(input.value, draft, 'retry must not overwrite another draft');
  }
}
"""
    )
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr

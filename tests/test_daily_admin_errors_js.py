"""Execute Daily Admin error renderers with untrusted provider text."""

import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parents[1] / "app/templates/admin_daily.html"


def _function(template, signature, *, module="", indent="  "):
    start = template.index(signature, template.index(module) if module else 0)
    end = template.index(f"\n{indent}}}", start) + len(indent) + 2
    return template[start:end]


@pytest.mark.parametrize(
    "module,name,target",
    [
        ("const BroadcastModule", "loadOverview", "channel-cards"),
        ("const BroadcastModule", "loadSubscribers", "sub-tbody"),
        ("const BroadcastModule", "loadOfferHistory", "offer-tbody"),
        ("const CrocModule", "loadPuzzles", "croc-puzzle-container"),
        ("const TriviaModule", "loadPuzzles", "trivia-puzzles-list"),
        ("const TriviaModule", "loadLeaderboard", "trivia-leaderboard-container"),
        ("const TarotModule", "load", "tarot-today-cards"),
    ],
)
def test_api_error_text_never_becomes_admin_markup(module, name, target):
    template = TEMPLATE.read_text(encoding="utf-8")
    escape = template[template.index("function escapeHtml(") : template.index("function showToast(")]
    source = _function(template, f"  async function {name}(", module=module)
    _run(
        escape
        + """
const assert = require('node:assert/strict');
const attack = '<img src=x onerror="globalThis.injected=true"> & private <detail>';
const nodes = new Map();
const document = {getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', style:{}, textContent:'', innerHTML:'', checked:false});
  return nodes.get(id);
}};
const getHeaders = {}, SUB_LIMIT = 50, OFFER_LIMIT = 50;
const _selectedUids = new Set();
const _updateBatchBar = () => {};
let selectedDate = null, currentLbType, subOffset, offerOffset;
const fetch = async () => { throw new Error(attack); };
"""
        + source
        + f"""
{name}().then(() => {{
  const html = nodes.get({target!r}).innerHTML;
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('&lt;img'), true);
  assert.equal(html.includes('&amp; private &lt;detail&gt;'), true);
}}).catch(error => {{ process.stderr.write(String(error)); process.exitCode = 1; }});
"""
    )


def test_toasts_display_server_messages_as_text():
    template = TEMPLATE.read_text(encoding="utf-8")
    source = template[template.index("function escapeHtml(") : template.index("function showConfirm(")]
    _run(
        """
const assert = require('node:assert/strict');
let toast;
const document = {
  getElementById: () => ({appendChild: node => { toast = node; }}),
  createElement: () => ({style:{}}),
};
const setTimeout = () => {};
"""
        + source
        + """
showToast('<img src=x onerror="globalThis.injected=true"> & warning', 'error');
assert.equal(toast.innerHTML.includes('<img'), false);
assert.equal(toast.innerHTML.includes('&lt;img'), true);
assert.equal(toast.innerHTML.includes('&amp; warning'), true);
"""
    )


def _run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

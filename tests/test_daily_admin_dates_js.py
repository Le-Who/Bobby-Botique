"""Calendar filtering is independent of browser timezone and DST offsets."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_recent_calendar_boundary_is_seven_iso_days_across_dst():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = (Path(__file__).parents[1] / "app/static/js/daily-trivia-editor.js").read_text(encoding="utf-8")
    start = source.index("  function renderPuzzles(")
    function = source[start : source.index("\n  }", start) + 4]
    script = (
        r"""
process.env.TZ='Europe/Kyiv';
const assert=require('node:assert/strict');
let selectedDate=null,puzzles=[];
const escapeHtml=String;
const container={innerHTML:'',querySelectorAll:()=>[]};
"""
        + function
        + r"""
renderPuzzles([{date:'2026-10-20'},{date:'2026-10-21'}],container,'2026-10-28');
const older=container.innerHTML.indexOf('Более старые');
assert.ok(older>=0);
assert.ok(container.innerHTML.indexOf('data-date="2026-10-20"')>older);
assert.ok(container.innerHTML.indexOf('data-date="2026-10-21"')<older);
"""
    )
    result = subprocess.run([node, "-e", script], encoding="utf-8", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr

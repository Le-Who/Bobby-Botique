"""Small, offline checks for the admin controls page and its draft lifecycle."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "app/static/js/controls.js"
TEMPLATE = ROOT / "app/templates/controls.html"


def test_controls_page_has_csrf_and_section_navigation():
    page = TEMPLATE.read_text(encoding="utf-8")
    assert 'name="csrf-token"' in page
    assert "js/controls.js" in page
    for section in ("processes", "prompts", "catalogs", "limits", "history"):
        assert f'id="{section}"' in page


def test_drafts_keep_their_original_revision_across_refresh_and_other_save():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    script = f"""
const assert = require('node:assert/strict');
const {{ createDraftManager }} = require({SCRIPT.as_posix()!r});
const drafts = createDraftManager();
drafts.setSnapshot({{revision: 7}});
drafts.setDraft('process', 'chat', {{models:['one']}});
drafts.setDraft('prompt', 'system', 'edited');
drafts.setSnapshot({{revision: 8}});
assert.equal(drafts.getDraft('process', 'chat').expected_revision, 7);
assert.equal(drafts.getDraft('prompt', 'system').expected_revision, 7);
drafts.clearDraft('process', 'chat');
assert.equal(drafts.getDraft('process', 'chat'), undefined);
assert.equal(drafts.getDraft('prompt', 'system').value, 'edited');
drafts.setDraft('process', 'chat', {{models:['two']}});
assert.equal(drafts.getDraft('process', 'chat').expected_revision, 8);
drafts.setSnapshot({{revision: 9}});
assert.equal(drafts.getDraft('process', 'chat').expected_revision, 8);
drafts.rebaseDraft('process', 'chat');
assert.equal(drafts.getDraft('process', 'chat').expected_revision, 9);
"""
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr


def test_controls_script_avoids_html_injection_and_uses_csrf():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "X-CSRF-Token" in script
    assert "`${endpoint}/preview`" in script
    assert "`${endpoint}/restore`" in script


def test_save_acknowledgement_preserves_edits_made_during_request():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    script = f"""
const assert = require('node:assert/strict');
const {{ createDraftManager }} = require({SCRIPT.as_posix()!r});
const drafts = createDraftManager();
drafts.setSnapshot({{revision: 7}});
drafts.setDraft('prompt', 'system', 'submitted');
const submitted = drafts.getDraft('prompt', 'system');
drafts.setDraft('prompt', 'system', 'typed while saving');
drafts.setDraft('prompt', 'other', 'unrelated edit');
drafts.setSnapshot({{revision: 9}});
drafts.acceptSavedDraft('prompt', 'system', submitted, 7, 8);
assert.equal(drafts.getDraft('prompt', 'system').value, 'typed while saving');
assert.equal(drafts.getDraft('prompt', 'system').expected_revision, 8);
assert.equal(drafts.getDraft('prompt', 'other').expected_revision, 7);

// Even returning to the submitted text is a newer edit, not the sent version.
const second = drafts.getDraft('prompt', 'system');
drafts.setDraft('prompt', 'system', 'submitted');
drafts.acceptSavedDraft('prompt', 'system', second, 8, 10);
assert.equal(drafts.getDraft('prompt', 'system').value, 'submitted');
assert.equal(drafts.getDraft('prompt', 'system').expected_revision, 10);
const final = drafts.getDraft('prompt', 'system');
drafts.acceptSavedDraft('prompt', 'system', final, 10, 11);
assert.equal(drafts.getDraft('prompt', 'system'), undefined);

// A reset may begin without a draft; subsequent typing must survive its result.
drafts.setSnapshot({{revision: 11}});
drafts.setDraft('limit', 'gemini-example', 17);
drafts.acceptSavedDraft('limit', 'gemini-example', undefined, 11, 12);
assert.equal(drafts.getDraft('limit', 'gemini-example').value, 17);
assert.equal(drafts.getDraft('limit', 'gemini-example').expected_revision, 12);

// An explicit rebase to a different revision must not be silently undone.
const prior = drafts.getDraft('limit', 'gemini-example');
drafts.setSnapshot({{revision: 15}});
drafts.rebaseDraft('limit', 'gemini-example');
drafts.setDraft('limit', 'gemini-example', 18);
drafts.acceptSavedDraft('limit', 'gemini-example', prior, 12, 13);
assert.equal(drafts.getDraft('limit', 'gemini-example').expected_revision, 15);
"""
    result = subprocess.run([node, "-"], input=script, encoding="utf-8", capture_output=True)
    assert result.returncode == 0, result.stderr

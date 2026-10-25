"""Memory deletion and Reader controls in the actual rendered browser pages."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from app.utils.reader_utils import extract_toc, markdown_to_reader_html

pytestmark = pytest.mark.browser


def _run_page(page_name: str, scenario: str) -> None:
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required UI regression needs Node.js")
        pytest.skip("UI regression needs Node.js")
    template = Environment(loader=FileSystemLoader(root / "app/templates"), autoescape=True).get_template(
        page_name + ".html"
    )
    body_html = (
        '<h1 id="overview">Overview</h1><p>Readable answer.</p>'
        + "<p>Independent browser fixture.</p>" * 50
        + '<h2 id="details">Details</h2><p>Second section.</p>'
    )
    toc = [
        {"level": "h1", "anchor": "overview", "text": "Overview"},
        {"level": "h2", "anchor": "details", "text": "Details"},
    ]
    if scenario == "untrusted_content":
        markdown = (
            "# </script><script>window.readerInjected=true</script>\n\n## Details\n\n"
            "[Unsafe](javascript:readerInjected=true) [Safe](https://example.test/read?a=1&b=2)"
        )
        toc = extract_toc(markdown)
        body_html = markdown_to_reader_html(markdown, toc)
    html = template.render(
        body_html=body_html,
        toc=toc,
        source_label="Synthetic test",
        uid="synthetic-reader",
        telegraph_fallback_url="",
    )
    result = subprocess.run(
        [node, "miniapp_reader_probe.cjs"],
        input=json.dumps({"pageName": page_name, "scenario": scenario, "html": html}),
        cwd=root / "tests/browser",
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    if result.returncode and "Cannot find module 'playwright'" in result.stderr:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") != "1":
            pytest.skip("UI regression needs locked Playwright dependencies")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        "undo",
        "timer_delete",
        "unload_flush",
        "delete_failure",
        "reload_delete",
        "navigate_delete",
        "reload_failure",
        "reload_undo",
    ],
)
def test_memory_delete_window_has_exact_request_and_visible_result(scenario):
    _run_page("miniapp", scenario)


@pytest.mark.parametrize(
    "scenario",
    [
        "tts_toggle",
        "tts_hidden",
        "tts_error",
        "tts_reload",
        "tts_navigate",
        "tts_close",
        "toc",
        "clipboard_error",
        "untrusted_content",
    ],
)
def test_reader_controls_have_observable_browser_results(scenario):
    _run_page("reader", scenario)

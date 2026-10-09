"""Dated offline coverage measurement harness; output goes to task-specific temporary storage.

Not an integration runner or an OS/network sandbox. Bootstrap synthetic test
credentials before imports, guard the repository .env and Python socket connects.
Allow only stdlib Windows socketpair plumbing for asyncio. Use from this dated
repository location; the report explains measured vs. unexecuted boundaries.
"""
# ruff: noqa: E402

from __future__ import annotations

import ast
import contextlib
import importlib.metadata
import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(
    os.environ.get("GEMAIBOT_AUDIT_OUTPUT_DIR", str(pathlib.Path(tempfile.gettempdir()) / "gemaibot-audit-2026-10-09"))
).resolve()
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for name in (
    "TEST_DATABASE_URL",
    "TEST_REDIS_URL",
    "GEMAIBOT_TEST_ORIGINAL_DATABASE_URL",
    "GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL",
    "GEMAIBOT_REQUIRE_BROWSER_TESTS",
):
    os.environ.pop(name, None)
# Mirror tests/conftest.py before any application import; values are synthetic.
root_tree = ast.parse((ROOT / "tests/conftest.py").read_text(encoding="utf-8"))
for node in root_tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_SAFE_TEST_ENV" for t in node.targets):
        os.environ.update(ast.literal_eval(node.value))
        break
import dotenv

original_dotenv_values = dotenv.dotenv_values


def safe_dotenv_values(dotenv_path=None, *args, **kwargs):
    if dotenv_path is not None and pathlib.Path(dotenv_path).resolve() == ROOT / ".env":
        return {}
    return original_dotenv_values(dotenv_path, *args, **kwargs)


dotenv.dotenv_values = safe_dotenv_values
blocked_connects = []


def offline_audit(event, args):
    if (
        event == "open"
        and args
        and isinstance(args[0], (str, bytes, os.PathLike))
        and pathlib.Path(os.fsdecode(args[0])).resolve() == ROOT / ".env"
    ):
        raise PermissionError("Coverage audit does not read the repository .env")
    if event == "socket.connect":
        # Windows asyncio uses a TCP socketpair internally; permit only stdlib socketpair.
        caller = sys._getframe(1)
        if caller.f_code.co_name == "_fallback_socketpair" and caller.f_globals.get("__name__") == "socket":
            return
        blocked_connects.append({"event": event})
        raise OSError("Coverage audit blocked a socket connection")


sys.addaudithook(offline_audit)
import pytest


class EvidencePlugin:
    def __init__(self):
        self.items = []
        self.reports = []
        self.deselected = []
        self.collection_errors = []

    def pytest_itemcollected(self, item):
        self.items.append(
            {
                "nodeid": item.nodeid,
                "markers": sorted({m.name for m in item.iter_markers()}),
                "fixtures": sorted(item.fixturenames),
            }
        )

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append({"nodeid": report.nodeid})

    def pytest_runtest_logreport(self, report):
        record = {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": round(report.duration, 6),
        }
        if report.skipped:
            if isinstance(report.longrepr, tuple):
                record["reason"] = str(report.longrepr[2])
            else:
                record["reason"] = "skipped"
        self.reports.append(record)


mode = sys.argv[1] if len(sys.argv) > 1 else "collect"
plugin = EvidencePlugin()
args = ["tests/", "--override-ini=addopts=", "--timeout=30", "-q", "--basetemp=" + str(OUT / (mode + "-tmp"))]
if mode == "collect":
    args += ["--collect-only"]
elif mode == "unit":
    os.environ["COVERAGE_CORE"] = "ctrace"
    os.environ["COVERAGE_FILE"] = str(OUT / ".coverage-unit")
    args += [
        "--ignore=tests/integration",
        "-m",
        "not integration",
        "--cov=app",
        "--cov=bot",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:" + str(OUT / "coverage.json"),
        "--cov-report=",
    ]
elif mode == "focus":
    args[0:1] = sys.argv[2:]
    args += ["-x"]
elif mode == "browser":
    os.environ["GEMAIBOT_REQUIRE_BROWSER_TESTS"] = "1"
    args += ["-m", "browser"]
else:
    raise ValueError(mode)
started = time.monotonic()
with (
    (OUT / (mode + ".log")).open("w", encoding="utf-8") as stream,
    contextlib.redirect_stdout(stream),
    contextlib.redirect_stderr(stream),
):
    code = pytest.main(args, plugins=[plugin])
summary = {
    "mode": mode,
    "exit_code": int(code),
    "duration_seconds": round(time.monotonic() - started, 3),
    "python": sys.version.split()[0],
    "versions": {
        name: importlib.metadata.version(name)
        for name in ("pytest", "pytest-cov", "coverage", "pytest-asyncio", "pytest-timeout", "pytest-xdist")
    },
    "arguments": args,
    "all_items": plugin.items,
    "deselected": plugin.deselected,
    "reports": plugin.reports,
    "collection_errors": plugin.collection_errors,
    "blocked_python_socket_connections": len(blocked_connects),
    "environment": "synthetic credentials; repository .env unavailable; TEST_DATABASE_URL and TEST_REDIS_URL unset; Python socket.connect blocked",
}
(OUT / (mode + "-evidence.json")).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
sys.stdout.write(
    json.dumps(
        {
            key: summary[key]
            for key in (
                "mode",
                "exit_code",
                "duration_seconds",
                "python",
                "versions",
                "blocked_python_socket_connections",
            )
        },
        ensure_ascii=False,
    )
    + "\n"
)
sys.stdout.write("Evidence: " + str(OUT / (mode + "-evidence.json")) + "\n")
sys.exit(int(code))

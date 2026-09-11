from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke_log_viewer.py"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


def test_generate_emits_unique_bounded_json_events(tmp_path: Path):
    output = tmp_path / "synthetic.ndjson"

    completed = _run(
        "generate",
        "--count",
        "25",
        "--run-id",
        "test-run",
        "--large-every",
        "10",
        "--output",
        str(output),
    )

    assert completed.returncode == 0, completed.stderr
    raw_lines = output.read_bytes().splitlines()
    events = [json.loads(line) for line in raw_lines]
    assert len(events) == 25
    assert len({event["event_id"] for event in events}) == 25
    assert all(len(event["event_id"]) == 32 for event in events)
    assert all(len(line) <= 32_768 for line in raw_lines)
    assert max(map(len, raw_lines)) >= 32_700
    assert all(event["message"] == "Синтетическая проверка" for event in events)


def test_stats_reports_only_aggregates(tmp_path: Path):
    source = tmp_path / "source.ndjson"
    source.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-09-11T10:00:00Z","message":"private-alpha"}',
                '{"timestamp":"2026-09-11T10:00:02Z","message":"private-beta-longer"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _run("stats", "--input", str(source))

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["events"] == 2
    assert report["duration_seconds"] == 2.0
    assert report["events_per_second"] == 1.0
    assert report["max_bytes"] > report["min_bytes"]
    assert set(report) == {
        "duration_seconds",
        "events",
        "events_per_second",
        "invalid_lines",
        "max_bytes",
        "mean_bytes",
        "min_bytes",
        "p50_bytes",
        "p95_bytes",
    }
    assert "private" not in completed.stdout


def test_extract_loki_response_is_bounded_and_reports_id_gaps(tmp_path: Path):
    response = tmp_path / "query.json"
    output = tmp_path / "events.ndjson"
    expected = tmp_path / "expected.txt"
    values = [[str(1_000_000_000 + index), json.dumps({"event_id": f"{index:032x}"})] for index in range(503)]
    values.append(values[-1])
    response.write_text(
        json.dumps(
            {"status": "success", "data": {"resultType": "streams", "result": [{"stream": {}, "values": values}]}}
        ),
        encoding="utf-8",
    )
    expected.write_text("\n".join(f"{index:032x}" for index in range(504)) + "\n", encoding="utf-8")

    completed = _run(
        "extract",
        "--input",
        str(response),
        "--output",
        str(output),
        "--expected-ids",
        str(expected),
        "--limit",
        "500",
    )

    assert completed.returncode == 0, completed.stderr
    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    report = json.loads(completed.stdout)
    assert len(events) == 500
    assert report == {"duplicates": 1, "exported": 500, "missing": 4, "received": 504}


def test_query_rejects_limits_or_windows_that_can_overload_the_viewer():
    too_many = _run(
        "query",
        "--loki-url",
        "http://127.0.0.1:3100",
        "--query",
        '{service_name="gemaibotv2"}',
        "--since",
        "15m",
        "--limit",
        "501",
    )
    too_wide = _run(
        "query",
        "--loki-url",
        "http://127.0.0.1:3100",
        "--query",
        '{service_name="gemaibotv2"}',
        "--since",
        "8d",
        "--limit",
        "100",
    )

    assert too_many.returncode == 2
    assert "limit must be between 1 and 500" in too_many.stderr
    assert too_wide.returncode == 2
    assert "window must not exceed 7 days" in too_wide.stderr

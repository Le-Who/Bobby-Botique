"""Exercise synthetic stdout through the protected, isolated collector stack.

Run against a disposable Docker daemon after starting ops/observability/compose.yml.
The --ephemeral flag is mandatory. Existing named workload containers are never
replaced. The probe removes only the container IDs it created, with an ownership
label check, and leaves a small synthetic manifest for an independent pytest run.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.smoke_log_viewer import MAX_EVENT_BYTES, generate_events  # noqa: E402

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
OWNER_LABEL = "com.gemaibot.coverage-run"


def loopback_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("the synthetic Grafana probe requires a loopback origin without credentials")
    return value.rstrip("/")


class StackProbe:
    def __init__(self, base_url: str, password_file: Path, manifest: dict):
        self.base_url = loopback_url(base_url)
        # Match the Docker entrypoint's `$(cat file)` semantics: remove final LF,
        # while preserving spaces/CR that are part of the configured password.
        password = password_file.read_bytes().rstrip(b"\n").decode("utf-8")
        if not password:
            raise ValueError("the disposable Grafana password must be nonempty")
        self.authorization = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode("ascii")
        self.manifest = manifest

    def request(self, path: str, *, authenticated: bool = True) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = self.authorization
        request = urllib.request.Request(self.base_url + path, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=10)  # noqa: S310 - validated loopback test origin
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise AssertionError("collector response exceeds the probe's bounded output")
            return response.code, payload

    def query(
        self,
        expression: str,
        *,
        limit: int = 500,
        days: int = 1,
        authenticated: bool = True,
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        end = end or datetime.now(UTC)
        start = start or end - timedelta(days=days)
        parameters = urllib.parse.urlencode(
            {
                "query": expression,
                "start": int(start.timestamp() * 1_000_000_000),
                "end": int(end.timestamp() * 1_000_000_000),
                "direction": "forward",
                "limit": limit,
            }
        )
        return self.request(
            "/api/datasources/proxy/uid/gemaibot-loki/loki/api/v1/query_range?" + parameters,
            authenticated=authenticated,
        )

    def rows(self, service: str) -> list[tuple[dict, str]]:
        run_literal = json.dumps(self.manifest["run_id"])
        status, payload = self.query(f'{{service_name="{service}"}} |= {run_literal}')
        assert status == 200, f"protected datasource query returned HTTP {status}"
        parsed = json.loads(payload)
        assert parsed["status"] == "success"
        assert parsed["data"]["resultType"] == "streams"
        return [(stream["stream"], line) for stream in parsed["data"]["result"] for _, line in stream["values"]]

    def check_auth(self):
        status, payload = self.query('{service_name="gemaibotv2"}', authenticated=False)
        assert status in {401, 403}, f"unauthenticated datasource leaked access: HTTP {status}"
        assert self.manifest["run_id"].encode() not in payload
        status, payload = self.request("/api/user")
        assert status == 200 and json.loads(payload)["login"] == "admin"

    def check_events(self, service: str):
        rows = self.rows(service)
        actual = {}
        for labels, line in rows:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("smoke_run_id") != self.manifest["run_id"]:
                continue
            assert labels["parse_status"] == "valid"
            assert labels["service_name"] == service
            assert labels["environment"] == "synthetic"
            assert event["schema_version"] == 1
            assert event["request_id"] == self.manifest["requests"][event["event_id"]]
            expected_source = (
                "scripts/generate_observability_private_probe.py"
                if event["event_id"] == self.manifest["privacy"]["event_id"]
                else "scripts/smoke_log_viewer.py"
            )
            assert event["source"]["file"] == expected_source
            assert event["event_id"] not in actual, "one stdout event was ingested more than once"
            actual[event["event_id"]] = line
        expected = set(self.manifest["expected"][service])
        assert set(actual) == expected, (
            f"{service}: missing={len(expected - set(actual))}, extra={len(set(actual) - expected)}"
        )
        if service == "gemaibotv2":
            assert max(len(line.encode("utf-8")) for line in actual.values()) == MAX_EVENT_BYTES

    def check_source_filters_and_fallback(self):
        for service in ("gemaibotv2", "ytdlbot"):
            rows = self.rows(service)
            wire = "\n".join(line for _, line in rows)
            for excluded in self.manifest["excluded"]:
                assert excluded not in wire, "collector accepted stdout outside its name/label allowlist"
            invalid = [
                (labels, line)
                for labels, line in rows
                if line == f"synthetic-invalid-{service}-{self.manifest['run_id']}"
            ]
            assert len(invalid) == 1
            assert invalid[0][0]["parse_status"] == "invalid"
            assert invalid[0][0]["service_name"] == service
            for _, line in rows:
                if self.manifest["run_id"] in line:
                    assert len(line.encode("utf-8")) <= MAX_EVENT_BYTES

    def check_query_limits(self):
        status, payload = self.query('{service_name="gemaibotv2"}', limit=501)
        assert status == 400 and b"500" in payload, f"server did not enforce the 500-entry query limit: HTTP {status}"
        # Loki clamps historical query_range requests to max_query_lookback;
        # this is distinct from max_query_length, which rejects long ranges.
        end = datetime.now(UTC)
        status, payload = self.query(
            '{service_name="gemaibotv2"}', start=end - timedelta(days=9), end=end - timedelta(days=8)
        )
        assert status == 200 and json.loads(payload)["data"]["result"] == []
        parameters = urllib.parse.urlencode({"query": 'sum(count_over_time({service_name="gemaibotv2"}[192h]))'})
        status, payload = self.request("/api/datasources/proxy/uid/gemaibot-loki/loki/api/v1/query?" + parameters)
        assert status == 400 and b"max_query_lookback" in payload
        status, payload = self.query('{service_name="gemaibotv2"}', start=end, end=end + timedelta(days=8))
        assert status == 400 and b"query time range exceeds the limit" in payload
        assert b"168" in payload or b"7d" in payload or b"1w" in payload

    def check_privacy(self):
        # This event was serialized by the actual app writer before stdout replay.
        # Alloy carries the protected result; redaction remains the app's contract.
        selected = []
        for _, line in self.rows("gemaibotv2"):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                event.get("event_id") == self.manifest["privacy"]["event_id"]
                and event.get("smoke_run_id") == self.manifest["run_id"]
            ):
                selected.append(event)
        assert len(selected) == 1
        event = selected[0]
        expected = self.manifest["privacy"]
        assert event["message"] == "Controlled provider echo [redacted]"
        assert event["credential_echo"] == {"api_key": "[redacted]"}
        assert event["key_present"] is True
        assert event["request_id"] == expected["request_id"]
        assert event["key_suffix"] == expected["key_suffix"]
        assert event["key_fingerprint"] == expected["key_fingerprint"]
        for name, prefix in (("birth", "birth"), ("private_memory", "memory")):
            fields = event[name]
            assert fields["content_policy"] == "forbidden"
            assert fields["content_chars"] == expected[prefix + "_chars"]
            assert fields["content_fingerprint"] == expected[prefix + "_fingerprint"]
            assert not {"content_text", "content_preview"} & fields.keys()


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *arguments], capture_output=True, encoding="utf-8", timeout=60, check=check)


def run_probe(
    *, base_url: str, password_file: Path, compose_file: Path, private_event_file: Path, output: Path
) -> dict:
    base_url = loopback_url(base_url)
    names = ("tg-bot", "ytdlbot-bot-1")
    for name in names:
        result = docker("inspect", name, check=False)
        if result.returncode == 0:
            raise RuntimeError(f"refusing to replace an existing workload container: {name}")
    image = re.search(r"image:\s+(haproxy:\S+)", compose_file.read_text(encoding="utf-8"))
    if image is None:
        raise ValueError("expected the pinned HAProxy workload image in the actual Compose config")
    run_id = uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"run_id": run_id, "expected": {}, "requests": {}, "excluded": []}
    private_payload = json.loads(private_event_file.read_text(encoding="utf-8"))
    private_event = private_payload["event"]
    private_event["smoke_run_id"] = run_id
    manifest["privacy"] = {"event_id": private_event["event_id"], **private_payload["expected"]}
    probe = StackProbe(base_url, password_file, manifest)
    probe.check_auth()
    owned: list[str] = []

    def remove(container_id: str):
        owner = docker("inspect", "--format", '{{index .Config.Labels "' + OWNER_LABEL + '"}}', container_id)
        if owner.stdout.strip() != run_id:
            raise RuntimeError("refusing to remove a container whose ownership label changed")
        docker("rm", "-f", container_id)
        owned.remove(container_id)

    def workload(name: str, data: bytes, *, collect: bool):
        path = output / (name + ".ndjson")
        path.write_bytes(data)
        arguments = ["run", "-d", "--name", name, "--label", f"{OWNER_LABEL}={run_id}"]
        if collect:
            arguments.extend(["--label", "com.gemaibot.logs=true"])
        arguments.extend(
            [
                "--mount",
                f"type=bind,source={path.resolve()},target=/probe.ndjson,readonly",
                "--entrypoint",
                "/bin/sh",
                image.group(1),
                "-c",
                "cat /probe.ndjson; sleep 240",
            ]
        )
        container_id = docker(*arguments).stdout.strip()
        owned.append(container_id)
        return container_id

    try:
        excluded_unlabelled = "synthetic-private-unlabelled-" + run_id
        excluded_other = "synthetic-private-other-project-" + run_id
        manifest["excluded"] = [excluded_unlabelled, excluded_other]
        unlabelled_id = workload("tg-bot", (excluded_unlabelled + "\n").encode(), collect=False)
        workload("other-project-" + run_id, (excluded_other + "\n").encode(), collect=True)
        # A complete discovery interval elapses with the correct name but no opt-in.
        time.sleep(17)
        remove(unlabelled_id)
        for service, name, count in (("gemaibotv2", "tg-bot", 120), ("ytdlbot", "ytdlbot-bot-1", 7)):
            lines = []
            expected = []
            for wire in generate_events(count=count, run_id=run_id, large_every=20 if service == "gemaibotv2" else 0):
                event = json.loads(wire)
                if service == "ytdlbot":
                    # Distinct IDs preserve exact cardinality across service streams.
                    event["service"] = service
                    event["event_id"] = "ytdl-" + event["event_id"]
                    wire = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                expected.append(event["event_id"])
                manifest["requests"][event["event_id"]] = event["request_id"]
                lines.append(wire)
            manifest["expected"][service] = expected
            if service == "gemaibotv2":
                expected.append(private_event["event_id"])
                manifest["requests"][private_event["event_id"]] = private_event["request_id"]
                lines.append(json.dumps(private_event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            lines.append(f"synthetic-invalid-{service}-{run_id}".encode())
            workload(name, b"\n".join(lines) + b"\n", collect=True)
        manifest_file = output / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        deadline = time.monotonic() + 60
        while True:
            try:
                probe.check_events("gemaibotv2")
                probe.check_events("ytdlbot")
                break
            except AssertionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        probe.check_auth()
        probe.check_source_filters_and_fallback()
        probe.check_query_limits()
        probe.check_privacy()
        return {"events": 128, "invalid_lines": 2, "excluded_sources": 2, "manifest": str(manifest_file)}
    finally:
        validation_error = sys.exception()
        cleanup_errors = []
        for container_id in list(owned):
            try:
                remove(container_id)
            except Exception as error:
                note = f"Synthetic workload cleanup failed for {container_id}: {type(error).__name__}"
                error.add_note(note)
                cleanup_errors.append(error)
                if validation_error is not None:
                    validation_error.add_note(note)
        if cleanup_errors and validation_error is None:
            raise ExceptionGroup("Synthetic workload cleanup failed", cleanup_errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ephemeral", action="store_true", help="Confirm a disposable test Docker daemon")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3000")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--private-event-file", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, default=ROOT / "ops/observability/compose.yml")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.ephemeral:
        parser.error("--ephemeral is required; this probe creates named synthetic workload containers")
    result = run_probe(
        base_url=args.grafana_url,
        password_file=args.password_file,
        compose_file=args.compose_file,
        private_event_file=args.private_event_file,
        output=args.output,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

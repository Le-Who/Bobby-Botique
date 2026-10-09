"""Actual synthetic probe cleanup, with only Docker/HTTP/time boundaries fake."""

import json
import subprocess
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_observability_stack as stack


@pytest.fixture
def probe_case(monkeypatch, tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-password\n", encoding="utf-8")
    private = tmp_path / "private.json"
    expected = {
        "request_id": "synthetic-private-request",
        "key_suffix": "0001",
        "key_fingerprint": "synthetic-key-fingerprint",
        "birth_chars": 7,
        "memory_chars": 9,
        "birth_fingerprint": "synthetic-birth-fingerprint",
        "memory_fingerprint": "synthetic-memory-fingerprint",
    }
    event = {
        "schema_version": 1,
        "event_id": "synthetic-private-event",
        "request_id": expected["request_id"],
        "source": {"file": "scripts/generate_observability_private_probe.py"},
        "message": "Controlled provider echo [redacted]",
        "credential_echo": {"api_key": "[redacted]"},
        "key_present": True,
        "key_suffix": expected["key_suffix"],
        "key_fingerprint": expected["key_fingerprint"],
        **{
            name: {
                "content_policy": "forbidden",
                "content_chars": expected[prefix + "_chars"],
                "content_fingerprint": expected[prefix + "_fingerprint"],
            }
            for name, prefix in (("birth", "birth"), ("private_memory", "memory"))
        },
    }
    private.write_text(json.dumps({"event": event, "expected": expected}), encoding="utf-8")
    case = SimpleNamespace(
        containers={},
        created=[],
        inspections=[],
        removals=[],
        failure=None,
        validation_fault=True,
        remaining_at_validation=None,
        output=tmp_path / "probe-output",
    )

    def docker(*args, check=True):
        if args[0] == "run":
            container_id = f"controlled-{len(case.created) + 1}"
            labels = [args[index + 1] for index, arg in enumerate(args) if arg == "--label"]
            owner = next(label.split("=", 1)[1] for label in labels if label.startswith(stack.OWNER_LABEL + "="))
            mount = args[args.index("--mount") + 1]
            source = mount.split("source=", 1)[1].split(",target=", 1)[0]
            case.created.append(container_id)
            case.containers[container_id] = {
                "name": args[args.index("--name") + 1],
                "owner": owner,
                "collect": "com.gemaibot.logs=true" in labels,
                "lines": Path(source).read_text(encoding="utf-8").splitlines(),
            }
            return subprocess.CompletedProcess(args, 0, stdout=container_id + "\n")
        if args[0] == "inspect" and "--format" not in args:
            assert check is False
            return subprocess.CompletedProcess(args, 1, stdout="")
        container_id = args[-1]
        if args[0] == "inspect":
            case.inspections.append(container_id)
            if container_id == "controlled-2" and case.failure == "inspect":
                raise subprocess.CalledProcessError(1, args, stderr="synthetic inspect failure")
            owner = case.containers[container_id]["owner"]
            if container_id == "controlled-2" and case.failure == "foreign":
                owner = "different-owner"
            return subprocess.CompletedProcess(args, 0, stdout=owner + "\n")
        assert args[:2] == ("rm", "-f")
        case.removals.append(container_id)
        if container_id == "controlled-2" and case.failure == "rm":
            raise subprocess.CalledProcessError(1, args, stderr="synthetic remove failure")
        case.containers.pop(container_id)
        return subprocess.CompletedProcess(args, 0, stdout=container_id)

    def request(self, path, *, authenticated=True):
        if not authenticated:
            return 401, b"authentication required"
        if path == "/api/user":
            return 200, b'{"login":"admin"}'
        parsed = urllib.parse.urlsplit(path)
        parameters = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/query"):
            return 400, b"max_query_lookback"
        if parameters["limit"] == ["501"]:
            case.remaining_at_validation = tuple(case.containers)
            return (200, b"unexpected successful validation") if case.validation_fault else (400, b"limit 500")
        start = int(parameters["start"][0])
        end = int(parameters["end"][0])
        if end - start > 7 * 86400 * 1_000_000_000:
            return 400, b"query time range exceeds the limit 168"
        streams = []
        if end > int(datetime.now(UTC).timestamp() * 1_000_000_000) - 7 * 86400 * 1_000_000_000:
            service = "ytdlbot" if 'service_name="ytdlbot"' in parameters["query"][0] else "gemaibotv2"
            name = "ytdlbot-bot-1" if service == "ytdlbot" else "tg-bot"
            for container in case.containers.values():
                if container["name"] != name or not container["collect"]:
                    continue
                for line in container["lines"]:
                    status = "valid" if line.startswith("{") else "invalid"
                    streams.append(
                        {
                            "stream": {"service_name": service, "environment": "synthetic", "parse_status": status},
                            "values": [[str(end), line]],
                        }
                    )
        return 200, json.dumps({"status": "success", "data": {"resultType": "streams", "result": streams}}).encode()

    monkeypatch.setattr(stack, "docker", docker)
    monkeypatch.setattr(stack.StackProbe, "request", request)
    monkeypatch.setattr(stack, "time", SimpleNamespace(sleep=lambda seconds: None, monotonic=lambda: 0))

    def run():
        return stack.run_probe(
            base_url="http://127.0.0.1:3000",
            password_file=password,
            compose_file=stack.ROOT / "ops/observability/compose.yml",
            private_event_file=private,
            output=case.output,
        )

    case.run = run
    return case


@pytest.mark.parametrize("failure", ["inspect", "rm", "foreign"])
def test_validation_fault_keeps_original_error_and_attempts_each_owned_cleanup(probe_case, failure):
    probe_case.failure = failure
    with pytest.raises(AssertionError, match="server did not enforce the 500-entry query limit") as validation:
        probe_case.run()
    assert probe_case.remaining_at_validation == ("controlled-2", "controlled-3", "controlled-4")
    assert probe_case.inspections == ["controlled-1", "controlled-2", "controlled-3", "controlled-4"]
    expected_removals = ["controlled-1", "controlled-3", "controlled-4"]
    if failure == "rm":
        expected_removals.insert(1, "controlled-2")
    assert probe_case.removals == expected_removals
    assert set(probe_case.containers) == {"controlled-2"}
    notes = "\n".join(validation.value.__notes__)
    assert "controlled-2" in notes
    assert ("RuntimeError" if failure == "foreign" else "CalledProcessError") in notes


def test_successful_validation_still_surfaces_cleanup_failure_after_other_owned_removals(probe_case):
    probe_case.validation_fault = False
    probe_case.failure = "rm"
    with pytest.raises(ExceptionGroup, match="cleanup") as cleanup:
        probe_case.run()
    assert probe_case.inspections == ["controlled-1", "controlled-2", "controlled-3", "controlled-4"]
    assert probe_case.removals == ["controlled-1", "controlled-2", "controlled-3", "controlled-4"]
    assert set(probe_case.containers) == {"controlled-2"}
    assert len(cleanup.value.exceptions) == 1
    assert isinstance(cleanup.value.exceptions[0], subprocess.CalledProcessError)
    assert "controlled-2" in "\n".join(cleanup.value.exceptions[0].__notes__)


def test_successful_probe_removes_only_its_owned_ids_and_keeps_manifest(probe_case):
    probe_case.validation_fault = False
    result = probe_case.run()
    assert result == {
        "events": 128,
        "invalid_lines": 2,
        "excluded_sources": 2,
        "manifest": str(probe_case.output / "manifest.json"),
    }
    assert probe_case.removals == ["controlled-1", "controlled-2", "controlled-3", "controlled-4"]
    assert probe_case.containers == {}
    manifest = json.loads((probe_case.output / "manifest.json").read_text(encoding="utf-8"))
    assert {service: len(events) for service, events in manifest["expected"].items()} == {
        "gemaibotv2": 121,
        "ytdlbot": 7,
    }


def test_current_probe_events_are_read_after_more_than_500_historical_events(monkeypatch, tmp_path):
    current_run = "a" * 32
    previous_run = "b" * 32
    current_line = next(stack.generate_events(count=1, run_id=current_run, large_every=1)).decode("utf-8")
    historical_line = next(stack.generate_events(count=1, run_id=previous_run)).decode("utf-8")
    event = json.loads(current_line)
    password = tmp_path / "password"
    password.write_text("synthetic-password\n", encoding="utf-8")
    manifest = {
        "run_id": current_run,
        "expected": {"gemaibotv2": [event["event_id"]]},
        "requests": {event["event_id"]: event["request_id"]},
        "privacy": {"event_id": "unused-privacy-event"},
    }

    def request(self, path, *, authenticated=True):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        assert query["limit"] == ["500"]
        scoped = query["query"][0].endswith('|= "' + current_run + '"')
        # A forward query without run filtering reaches its server limit on
        # previous events; current stdout is already ingested but not returned.
        lines = [current_line] if scoped else [historical_line] * 500
        response = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service_name": "gemaibotv2", "environment": "synthetic", "parse_status": "valid"},
                        "values": [["1", line] for line in lines],
                    }
                ],
            },
        }
        return 200, json.dumps(response).encode()

    monkeypatch.setattr(stack.StackProbe, "request", request)
    stack.StackProbe("http://127.0.0.1:3000", password, manifest).check_events("gemaibotv2")

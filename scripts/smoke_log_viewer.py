"""Generate and inspect synthetic data for the protected log viewer.

This utility never connects to Telegram, a provider, Redis, or PostgreSQL. Query
and export limits deliberately match the observability stack's server-side limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_EVENT_BYTES = 32_768
MAX_QUERY_EVENTS = 500
MAX_QUERY_WINDOW = timedelta(days=7)
_DURATION_PATTERN = re.compile(r"(?P<amount>[1-9][0-9]*)(?P<unit>[smhd])")


def _wire(event: dict[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _stable_id(run_id: str, index: int, purpose: str) -> str:
    value = f"{run_id}:{index}:{purpose}".encode()
    return hashlib.blake2s(value, digest_size=16).hexdigest()


def _padded_wire(event: dict[str, Any], target_bytes: int) -> bytes:
    padded = {**event, "synthetic_padding": ""}
    empty_size = len(_wire(padded))
    if target_bytes < empty_size:
        raise ValueError(f"target size {target_bytes} is smaller than the event envelope {empty_size}")
    padded["synthetic_padding"] = "x" * (target_bytes - empty_size)
    encoded = _wire(padded)
    if len(encoded) != target_bytes:
        raise AssertionError("synthetic event padding did not reach the requested byte size")
    return encoded


def generate_events(
    *,
    count: int,
    run_id: str,
    large_every: int = 0,
    max_event_bytes: int = MAX_EVENT_BYTES,
) -> Iterable[bytes]:
    if count < 1:
        raise ValueError("count must be positive")
    if large_every < 0:
        raise ValueError("large_every must not be negative")

    started = datetime.now(UTC) - timedelta(milliseconds=count)
    for index in range(count):
        event = {
            "schema_version": 1,
            "timestamp": (started + timedelta(milliseconds=index)).isoformat().replace("+00:00", "Z"),
            "level": "error" if index % 100 == 99 else "info",
            "event": "synthetic.log_viewer_probe",
            "message": "Синтетическая проверка",
            "event_id": _stable_id(run_id, index, "event"),
            "service": "gemaibotv2",
            "environment": "synthetic",
            "release": "synthetic",
            "instance_id": "synthetic-instance",
            "logger": "smoke_log_viewer",
            "source": {"file": "scripts/smoke_log_viewer.py", "function": "generate_events", "line": 0},
            "request_id": _stable_id(run_id, index, "request"),
            "trace_id": _stable_id(run_id, index, "trace"),
            "actual_model": "synthetic-model",
            "provider": "synthetic",
            "user_id": index + 1,
            "smoke_run_id": run_id,
            "smoke_sequence": index,
        }
        if large_every and (index + 1) % large_every == 0:
            yield _padded_wire(event, max_event_bytes)
        else:
            yield _wire(event)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def ndjson_stats(path: Path) -> dict[str, int | float]:
    sizes: list[int] = []
    timestamps: list[datetime] = []
    invalid_lines = 0
    with path.open("rb") as stream:
        for raw_line in stream:
            line = raw_line.rstrip(b"\r\n")
            if not line:
                continue
            sizes.append(len(line))
            try:
                event = json.loads(line)
                timestamp = event.get("timestamp")
                if not isinstance(timestamp, str):
                    raise ValueError("missing timestamp")
                timestamps.append(_parse_timestamp(timestamp))
            except json.JSONDecodeError, UnicodeDecodeError, ValueError, AttributeError:
                invalid_lines += 1

    duration = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) > 1 else 0.0
    return {
        "events": len(sizes),
        "invalid_lines": invalid_lines,
        "duration_seconds": duration,
        "events_per_second": round(len(sizes) / duration, 3) if duration > 0 else 0.0,
        "min_bytes": min(sizes, default=0),
        "mean_bytes": round(sum(sizes) / len(sizes), 3) if sizes else 0.0,
        "p50_bytes": _percentile(sizes, 0.50),
        "p95_bytes": _percentile(sizes, 0.95),
        "max_bytes": max(sizes, default=0),
    }


def _duration(value: str) -> timedelta:
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("duration must use an integer followed by s, m, h, or d")
    amount = int(match.group("amount"))
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return timedelta(seconds=amount * multipliers[match.group("unit")])


def _validate_query_bounds(limit: int, window: timedelta) -> None:
    if not 1 <= limit <= MAX_QUERY_EVENTS:
        raise ValueError("limit must be between 1 and 500")
    if window > MAX_QUERY_WINDOW:
        raise ValueError("window must not exceed 7 days")


def query_loki(*, base_url: str, query: str, window: timedelta, limit: int, timeout: float = 10.0) -> bytes:
    _validate_query_bounds(limit, window)
    end = datetime.now(UTC)
    start = end - window
    parameters = urllib.parse.urlencode(
        {
            "query": query,
            "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)),
            "direction": "backward",
            "limit": str(limit),
        }
    )
    url = f"{base_url.rstrip('/')}/loki/api/v1/query_range?{parameters}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied Loki URL
        return response.read()


def extract_loki_response(
    payload: dict[str, Any],
    *,
    limit: int,
    expected_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    _validate_query_bounds(limit, timedelta())
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "streams":
        raise ValueError("input is not a successful Loki streams response")

    received: list[tuple[int, str]] = []
    for stream in payload["data"].get("result", []):
        for timestamp, line in stream.get("values", []):
            received.append((int(timestamp), line))
    received.sort(key=lambda item: item[0], reverse=True)

    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    duplicates = 0
    for _timestamp, line in received:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = {
                "event": "collector.invalid_json",
                "raw_line_fingerprint": hashlib.sha256(line.encode()).hexdigest(),
            }
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id in event_ids:
            duplicates += 1
            continue
        if isinstance(event_id, str):
            event_ids.add(event_id)
        if len(events) < limit:
            events.append(event)

    exported_ids = {event["event_id"] for event in events if isinstance(event.get("event_id"), str)}
    missing = len(expected_ids - exported_ids) if expected_ids is not None else 0
    report = {
        "received": len(received),
        "exported": len(events),
        "duplicates": duplicates,
        "missing": missing,
    }
    return events, report


def _write_lines(path: Path, lines: Iterable[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for line in lines:
            stream.write(line)
            stream.write(b"\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="write bounded synthetic NDJSON")
    generate.add_argument("--count", type=int, required=True)
    generate.add_argument("--run-id", default=datetime.now(UTC).strftime("smoke-%Y%m%dT%H%M%SZ"))
    generate.add_argument("--large-every", type=int, default=1000)
    generate.add_argument("--output", type=Path, required=True)

    stats = commands.add_parser("stats", help="print content-free NDJSON aggregates")
    stats.add_argument("--input", type=Path, required=True)

    query = commands.add_parser("query", help="make one bounded Loki range query")
    query.add_argument("--loki-url", required=True)
    query.add_argument("--query", required=True)
    query.add_argument("--since", default="15m")
    query.add_argument("--limit", type=int, default=100)
    query.add_argument("--output", type=Path, required=False)

    extract = commands.add_parser("extract", help="convert a Loki streams response to clean NDJSON")
    extract.add_argument("--input", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--expected-ids", type=Path)
    extract.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            _write_lines(
                args.output,
                generate_events(count=args.count, run_id=args.run_id, large_every=args.large_every),
            )
            print(json.dumps({"events": args.count, "output": str(args.output)}, ensure_ascii=False))
            return 0
        if args.command == "stats":
            print(json.dumps(ndjson_stats(args.input), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "query":
            window = _duration(args.since)
            _validate_query_bounds(args.limit, window)
            payload = query_loki(base_url=args.loki_url, query=args.query, window=window, limit=args.limit)
            if args.output is None:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.write(b"\n")
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(payload)
                print(json.dumps({"bytes": len(payload), "output": str(args.output)}))
            return 0
        if args.command == "extract":
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            expected_ids = None
            if args.expected_ids is not None:
                expected_ids = {
                    line.strip() for line in args.expected_ids.read_text(encoding="utf-8").splitlines() if line.strip()
                }
            events, report = extract_loki_response(payload, limit=args.limit, expected_ids=expected_ids)
            _write_lines(args.output, (_wire(event) for event in events))
            print(json.dumps(report, sort_keys=True))
            return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create a bounded, sanitized incident bundle from saved NDJSON or stdin."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.observability.incident import (
    IncidentCriteria,
    IncidentExportError,
    export_incident,
    parse_rfc3339,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="-", help="Saved NDJSON path, or - for stdin")
    parser.add_argument("--request-id")
    parser.add_argument("--error-id")
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--since", type=parse_rfc3339)
    parser.add_argument("--until", type=parse_rfc3339)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--include-identifiers", action="store_true")
    parser.add_argument("--include-content", action="store_true")
    parser.add_argument("--ci-summary", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    criteria = IncidentCriteria(
        request_id=args.request_id,
        error_id=args.error_id,
        user_id=args.user_id,
        since=args.since,
        until=args.until,
    )
    source = sys.stdin if args.input == "-" else open(args.input, encoding="utf-8")  # noqa: SIM115
    try:
        manifest = export_incident(
            source,
            source_name=args.input,
            criteria=criteria,
            output_dir=args.output,
            include_identifiers=args.include_identifiers,
            include_content=args.include_content,
        )
    except (IncidentExportError, OSError) as error:
        print(f"incident export failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        if source is not sys.stdin:
            source.close()
    if args.ci_summary:
        print(
            json.dumps(
                {
                    "selected_events": manifest["selected_events"],
                    "invalid_lines": manifest["invalid_lines"],
                    "duplicate_event_ids": manifest["duplicate_event_ids"],
                    "missing_terminal_count": len(manifest["missing_terminals"]),
                },
                separators=(",", ":"),
            )
        )
    else:
        print(f"Incident bundle written to {args.output} ({manifest['selected_events']} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

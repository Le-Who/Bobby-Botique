from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live natal report smoke check.")
    parser.add_argument("--webhook-url", default=os.getenv("WEBHOOK_URL", ""), help="Public bot WEBHOOK_URL.")
    parser.add_argument("--user-id", type=int, default=0, help="Synthetic user id for the smoke report.")
    parser.add_argument("--chat-id", type=int, default=0, help="Synthetic chat id for the smoke report.")
    args = parser.parse_args()

    if not args.webhook_url:
        sys.stderr.write("WEBHOOK_URL is required. Pass --webhook-url or set WEBHOOK_URL.\n")
        return 2

    from app.natal.smoke import run_natal_smoke

    result = asyncio.run(run_natal_smoke(args.webhook_url, user_id=args.user_id, chat_id=args.chat_id))
    sys.stdout.write(f"OK report_id={result.report_id}\n")
    sys.stdout.write(f"hosted_url={result.hosted_url}\n")
    if result.telegraph_url:
        sys.stdout.write(f"telegraph_url={result.telegraph_url}\n")
    sys.stdout.write(f"planets={result.planet_count} sections={result.section_count}\n")
    sys.stdout.write(
        "hosted_html="
        f"svg:{str(result.hosted_html_contains_svg).lower()} "
        f"sections:{str(result.hosted_html_contains_sections).lower()}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

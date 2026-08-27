from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.natal.city_readiness import check_city_catalog_readiness, format_city_readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local natal city catalog readiness.")
    parser.add_argument(
        "--max-warmup-ms", type=float, default=None, help="Fail if catalog warmup exceeds this many ms."
    )
    parser.add_argument(
        "--max-search-ms", type=float, default=None, help="Fail if any release city search exceeds this many ms."
    )
    parser.add_argument("--min-city-count", type=int, default=30000, help="Fail if local catalog has fewer cities.")
    args = parser.parse_args(argv)
    result = check_city_catalog_readiness(
        max_warmup_ms=args.max_warmup_ms,
        max_search_ms=args.max_search_ms,
        min_city_count=args.min_city_count,
    )
    sys.stdout.write(format_city_readiness(result))
    sys.stdout.write("\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

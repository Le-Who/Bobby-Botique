from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database import db_manager
from app.natal.accuracy import format_accuracy_results, load_golden_cases_from_json, validate_golden_cases
from app.natal.city_readiness import check_city_catalog_readiness, format_city_readiness
from app.natal.config_readiness import check_natal_config_readiness, format_natal_config_readiness
from app.natal.horizons_accuracy import format_horizons_results, validate_planets_against_horizons
from app.natal.smoke import run_natal_smoke
from app.natal.storage import check_storage_ready


async def _main(
    *,
    require_external: bool,
    check_storage: bool,
    webhook_url: str,
    user_id: int,
    chat_id: int,
    max_city_warmup_ms: float | None,
    max_city_search_ms: float | None,
    min_city_count: int,
    check_horizons: bool,
    check_config: bool,
    run_smoke: bool,
    fixture_path: str | Path | None = None,
) -> int:
    if require_external and fixture_path is None:
        sys.stdout.write("--require-external requires --reference-fixtures with independently verified cases.\n")
        return 1
    exit_code = 0
    cases = load_golden_cases_from_json(fixture_path) if fixture_path else None

    # Initialize DB pool for storage/smoke checks to prevent auto-reconnect warnings
    if check_storage or run_smoke:
        await db_manager.create_pool()

    city_result = check_city_catalog_readiness(
        max_warmup_ms=max_city_warmup_ms,
        max_search_ms=max_city_search_ms,
        min_city_count=min_city_count,
    )
    sys.stdout.write(format_city_readiness(city_result))
    sys.stdout.write("\n")
    if not city_result.passed:
        exit_code = 1

    accuracy_results = await validate_golden_cases(cases) if cases is not None else await validate_golden_cases()
    sys.stdout.write(format_accuracy_results(accuracy_results))
    sys.stdout.write("\n")
    if not accuracy_results:
        sys.stdout.write("No natal accuracy cases were checked.\n")
        exit_code = 1
    if not all(result.passed for result in accuracy_results):
        exit_code = 1
    if require_external and not all(result.externally_verified for result in accuracy_results):
        sys.stdout.write("External accuracy verification is required before public release.\n")
        exit_code = 1

    if check_horizons:
        horizons_results = (
            await validate_planets_against_horizons(cases=cases)
            if cases is not None
            else await validate_planets_against_horizons()
        )
        sys.stdout.write(format_horizons_results(horizons_results))
        sys.stdout.write("\n")
        if not all(result.passed for result in horizons_results):
            exit_code = 1

    if check_config:
        config_result = check_natal_config_readiness(settings, webhook_url=webhook_url)
        sys.stdout.write(format_natal_config_readiness(config_result))
        sys.stdout.write("\n")
        if not config_result.passed:
            return 1

    if check_storage:
        await check_storage_ready()
        sys.stdout.write("storage=ready\n")

    if run_smoke:
        if not webhook_url:
            sys.stdout.write("WEBHOOK_URL is required for --smoke.\n")
            return 1
        smoke_result = await run_natal_smoke(webhook_url, user_id=user_id, chat_id=chat_id)
        sys.stdout.write(f"smoke_report_id={smoke_result.report_id}\n")
        sys.stdout.write(f"smoke_hosted_url={smoke_result.hosted_url}\n")
        sys.stdout.write(
            "smoke_hosted_html="
            f"svg:{str(smoke_result.hosted_html_contains_svg).lower()} "
            f"sections:{str(smoke_result.hosted_html_contains_sections).lower()}\n"
        )

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run natal chart readiness checks.")
    parser.add_argument(
        "--require-external",
        action="store_true",
        help="Fail unless every accuracy golden case is independently verified.",
    )
    parser.add_argument(
        "--check-storage",
        action="store_true",
        help="Verify the natal_reports table exists without generating a report.",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("WEBHOOK_URL", ""),
        help="Public bot WEBHOOK_URL used by config checks and live smoke.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Generate a live sample report through storage and hosted report retrieval.",
    )
    parser.add_argument("--user-id", type=int, default=0, help="Synthetic user id for live smoke.")
    parser.add_argument("--chat-id", type=int, default=0, help="Synthetic chat id for live smoke.")
    parser.add_argument(
        "--max-city-warmup-ms",
        type=float,
        default=None,
        help="Fail if local city catalog warmup exceeds this many ms.",
    )
    parser.add_argument(
        "--max-city-search-ms",
        type=float,
        default=None,
        help="Fail if any release city search exceeds this many ms.",
    )
    parser.add_argument(
        "--min-city-count",
        type=int,
        default=30000,
        help="Fail if the local city catalog has fewer cities.",
    )
    parser.add_argument(
        "--check-horizons",
        action="store_true",
        help="Also compare planet longitudes against NASA/JPL Horizons API.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Verify natal release configuration, including feature flag, privacy, geocoder, web server, and WEBHOOK_URL.",
    )
    parser.add_argument(
        "--reference-fixtures",
        type=Path,
        default=None,
        help="Load externally verified natal accuracy fixture cases from a UTF-8 JSON file.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _main(
            require_external=args.require_external,
            check_storage=args.check_storage,
            webhook_url=args.webhook_url.strip(),
            user_id=args.user_id,
            chat_id=args.chat_id,
            max_city_warmup_ms=args.max_city_warmup_ms,
            max_city_search_ms=args.max_city_search_ms,
            min_city_count=args.min_city_count,
            check_horizons=args.check_horizons,
            check_config=args.check_config,
            run_smoke=args.smoke,
            fixture_path=args.reference_fixtures,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

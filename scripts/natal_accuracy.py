from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.natal.accuracy import (
    export_golden_cases_template,
    format_accuracy_results,
    load_golden_cases_from_json,
    validate_golden_cases,
)
from app.natal.horizons_accuracy import format_horizons_results, validate_planets_against_horizons


async def _main(
    *,
    require_external: bool = False,
    check_horizons: bool = False,
    fixture_path: str | Path | None = None,
    export_template_path: str | Path | None = None,
) -> int:
    if export_template_path is not None:
        export_golden_cases_template(export_template_path)
        sys.stdout.write(f"Natal accuracy fixture template exported to {export_template_path}.\n")
        return 0
    if require_external and fixture_path is None:
        sys.stdout.write("--require-external requires --reference-fixtures with independently verified cases.\n")
        return 1
    cases = load_golden_cases_from_json(fixture_path) if fixture_path else None
    results = await validate_golden_cases(cases) if cases is not None else await validate_golden_cases()
    sys.stdout.write(format_accuracy_results(results))
    sys.stdout.write("\n")
    if not results:
        sys.stdout.write("No natal accuracy cases were checked.\n")
        return 1
    if not all(result.passed for result in results):
        return 1
    if check_horizons:
        horizons_results = (
            await validate_planets_against_horizons(cases=cases)
            if cases is not None
            else await validate_planets_against_horizons()
        )
        sys.stdout.write(format_horizons_results(horizons_results))
        sys.stdout.write("\n")
        if not all(result.passed for result in horizons_results):
            return 1
    if require_external and not all(result.externally_verified for result in results):
        sys.stdout.write("External accuracy verification is required before public release.\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate natal chart calculation against golden cases.")
    parser.add_argument(
        "--require-external",
        action="store_true",
        help="Fail unless every golden case has been independently verified.",
    )
    parser.add_argument(
        "--check-horizons",
        action="store_true",
        help="Also compare planet longitudes against NASA/JPL Horizons API.",
    )
    parser.add_argument(
        "--reference-fixtures",
        type=Path,
        default=None,
        help="Load externally verified natal accuracy fixture cases from a UTF-8 JSON file.",
    )
    parser.add_argument(
        "--export-template",
        type=Path,
        default=None,
        help="Write a UTF-8 JSON fixture template from current golden cases and exit.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _main(
            require_external=args.require_external,
            check_horizons=args.check_horizons,
            fixture_path=args.reference_fixtures,
            export_template_path=args.export_template,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

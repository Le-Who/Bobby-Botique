from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _main(ttl_days: int | None) -> int:
    from app.config import settings
    from app.database import db_manager
    from app.natal.storage import check_storage_ready, purge_expired_reports

    if _uses_real_storage_function(check_storage_ready) or _uses_real_storage_function(purge_expired_reports):
        await db_manager.create_pool()
    effective_ttl_days = ttl_days if ttl_days is not None else int(getattr(settings, "NATAL_REPORT_TTL_DAYS", 365))
    await check_storage_ready()
    deleted_count = await purge_expired_reports(effective_ttl_days)
    sys.stdout.write(f"OK ttl_days={effective_ttl_days} deleted={deleted_count}\n")
    return 0


def _uses_real_storage_function(func) -> bool:
    return getattr(func, "__module__", "") == "app.natal.storage"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run natal report storage maintenance.")
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=None,
        help="Override NATAL_REPORT_TTL_DAYS for this cleanup run.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.ttl_days))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# ruff: noqa: T201
"""Standalone migration runner for CI/CD and manual use.

Usage:
    # Apply all pending migrations (exit 1 on failure):
    python scripts/migrate.py

    # Dry-run: show what would be applied, exit 1 if drift detected:
    python scripts/migrate.py --check

    # Show current migration status:
    python scripts/migrate.py --status

Environment:
    DATABASE_URL — required, Postgres connection string.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys

# Allow running from repo root without installing the package
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import asyncpg

from app.db.migration_manifest import (
    MigrationManifestError,
    discover_migration_files,
    migration_version,
)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("migrate")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _version(sql_file: pathlib.Path) -> str:
    """Extract the version prefix from a migration filename (e.g. '038b_...' → '038b')."""
    return migration_version(sql_file)


def _discover_files() -> list[pathlib.Path]:
    """Return the validated migration manifest."""
    return discover_migration_files(MIGRATIONS_DIR)


async def _ensure_tracking_table(conn: asyncpg.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            filename   TEXT NOT NULL,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)


async def _applied_versions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    return {row["version"] for row in rows}


async def _pending(conn: asyncpg.Connection) -> list[pathlib.Path]:
    applied = await _applied_versions(conn)
    return [f for f in _discover_files() if _version(f) not in applied]


# ── Modes ─────────────────────────────────────────────────────────────────────


async def run_check(conn: asyncpg.Connection) -> bool:
    """Dry-run: print drift, return True if no drift."""
    pending = await _pending(conn)
    if not pending:
        log.info("✓ No pending migrations — schema is up to date.")
        return True

    log.error("✗ Schema drift detected! %d migration(s) not applied:", len(pending))
    for f in pending:
        log.error("  · %s", f.name)
    return False


async def run_status(conn: asyncpg.Connection) -> None:
    """Print table of applied / pending migrations."""
    applied = await _applied_versions(conn)
    files = _discover_files()

    print(f"\n{'VERSION':<10} {'STATUS':<10} FILENAME")
    print("-" * 60)
    for f in files:
        v = _version(f)
        status = "applied" if v in applied else "PENDING"
        marker = "✓" if v in applied else "⚠"
        print(f"{v:<10} {marker} {status:<8} {f.name}")

    pending_count = sum(1 for f in files if _version(f) not in applied)
    print(f"\nTotal: {len(files)} files, {len(applied)} applied, {pending_count} pending.\n")


async def run_apply(conn: asyncpg.Connection) -> bool:
    """Apply all pending migrations. Return True if all succeeded."""
    pending = await _pending(conn)
    if not pending:
        log.info("✓ No pending migrations.")
        return True

    log.info("Applying %d pending migration(s)...", len(pending))
    all_ok = True

    for sql_file in pending:
        v = _version(sql_file)
        log.info("→ Applying %s ...", sql_file.name)
        try:
            sql_content = sql_file.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql_content)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, filename) VALUES ($1, $2)"
                    " ON CONFLICT (version) DO NOTHING",
                    v,
                    sql_file.name,
                )
            log.info("  ✓ %s applied", v)
        except Exception as exc:
            log.error("  ✗ %s FAILED: %s", sql_file.name, exc)
            all_ok = False
            # Hard stop — don't apply dependant migrations on a broken predecessor
            break

    return all_ok


# ── Entry point ───────────────────────────────────────────────────────────────


async def main(args: argparse.Namespace) -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        log.error("DATABASE_URL environment variable is not set.")
        return 1

    try:
        _discover_files()
    except MigrationManifestError as exc:
        log.error("Invalid migration manifest: %s", exc)
        return 1

    log.info("Connecting to database...")
    try:
        conn = await asyncpg.connect(database_url, statement_cache_size=0)
    except Exception as exc:
        log.error("Cannot connect to database: %s", exc)
        return 1

    try:
        await _ensure_tracking_table(conn)

        if args.mode == "check":
            ok = await run_check(conn)
            return 0 if ok else 1

        if args.mode == "status":
            await run_status(conn)
            return 0

        # Default: apply
        ok = await run_apply(conn)
        if not ok:
            log.error("One or more migrations failed. Review errors above.")
            return 1

        log.info("✓ All migrations applied successfully.")
        return 0

    finally:
        await conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Schema migration runner")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        dest="mode",
        action="store_const",
        const="check",
        help="Dry-run: exit 1 if any pending migrations exist (for CI gate)",
    )
    group.add_argument(
        "--status",
        dest="mode",
        action="store_const",
        const="status",
        help="Print applied / pending migration table",
    )
    parser.set_defaults(mode="apply")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))

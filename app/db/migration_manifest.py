"""Validation and deterministic discovery for numbered SQL migrations."""

from __future__ import annotations

import re
from pathlib import Path

_MIGRATION_FILENAME_RE = re.compile(r"^(?P<version>[0-9]{3}[a-z]?)_[a-z0-9_]+\.sql$")


class MigrationManifestError(RuntimeError):
    """Raised when migration files cannot form an unambiguous manifest."""


def migration_version(sql_file: Path) -> str:
    """Return the validated version prefix from a migration path."""
    match = _MIGRATION_FILENAME_RE.fullmatch(sql_file.name)
    if match is None:
        raise MigrationManifestError(f"invalid migration filename: {sql_file.name}")
    return match.group("version")


def discover_migration_files(migrations_dir: Path) -> list[Path]:
    """Return a validated, deterministic migration manifest."""
    if not migrations_dir.is_dir():
        raise MigrationManifestError(f"migrations directory not found: {migrations_dir}")

    sql_files = sorted(migrations_dir.glob("*.sql"), key=lambda path: path.name)
    if not sql_files:
        raise MigrationManifestError(f"no migration files found in: {migrations_dir}")

    versions: dict[str, Path] = {}
    for sql_file in sql_files:
        version = migration_version(sql_file)
        previous = versions.get(version)
        if previous is not None:
            raise MigrationManifestError(f"duplicate migration version {version}: {previous.name}, {sql_file.name}")
        versions[version] = sql_file

        try:
            sql = sql_file.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationManifestError(f"migration is not valid UTF-8: {sql_file.name}") from exc
        if not sql.strip():
            raise MigrationManifestError(f"empty migration: {sql_file.name}")

    return sql_files

"""Create a truthful production license inventory from a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import tomllib
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

MetadataRecord = Message | importlib.metadata.PackageMetadata
MetadataLoader = Callable[[str], MetadataRecord]
_LICENSE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")


def _optional_value(metadata: MetadataRecord, name: str) -> str | None:
    value = metadata.get(name)
    if value is None or not value.strip() or value.strip().upper() == "UNKNOWN":
        return None
    return value.strip()


def _denied_matches(
    expression: str | None,
    license_value: str | None,
    classifiers: list[str],
    denylist: tuple[str, ...],
) -> list[str]:
    candidates: set[str] = set()
    for value in (expression, license_value, *classifiers):
        if not value:
            continue
        candidates.add(value.casefold())
        candidates.update(token.casefold() for token in _LICENSE_TOKEN.findall(value))
        candidates.update(part.strip().casefold() for part in value.split("::"))
    return sorted(denied for denied in denylist if denied.casefold() in candidates)


def build_inventory(
    components: list[dict[str, Any]],
    metadata_loader: MetadataLoader,
    *,
    denylist: tuple[str, ...],
) -> dict[str, Any]:
    packages: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda item: str(item.get("name", "")).casefold()):
        name = str(component["name"])
        version = str(component["version"])
        try:
            metadata = metadata_loader(name)
        except Exception:
            metadata = Message()
        expression = _optional_value(metadata, "License-Expression")
        license_value = _optional_value(metadata, "License")
        classifiers = sorted(
            value.strip() for value in metadata.get_all("Classifier", []) if value.strip().startswith("License ::")
        )
        status = "known" if expression or license_value or classifiers else "unknown"
        packages.append(
            {
                "name": name,
                "version": version,
                "license_expression": expression,
                "license": license_value,
                "classifiers": classifiers,
                "status": status,
            }
        )
        denied = _denied_matches(expression, license_value, classifiers, denylist)
        if denied:
            violations.append({"name": name, "version": version, "denied": denied})

    return {
        "schema_version": 1,
        "denylist": list(denylist),
        "unknown_count": sum(package["status"] == "unknown" for package in packages),
        "violations": violations,
        "packages": packages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
    project = tomllib.loads(args.project.read_text(encoding="utf-8"))
    denylist = tuple(str(item) for item in project["tool"]["dependency-frontier"]["license-denylist"])
    report = build_inventory(sbom.get("components", []), importlib.metadata.metadata, denylist=denylist)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 2 if report["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

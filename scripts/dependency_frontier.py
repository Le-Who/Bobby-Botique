"""Read-only dependency frontier discovery and reporting utilities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version


class ChangeKind(StrEnum):
    NONE = "none"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    MAJOR_RISK = "major-risk"
    DOWNGRADE = "downgrade"


class LiveOutcome(StrEnum):
    NOT_RUN = "not-run"
    PASSED = "passed"
    CANDIDATE_FAILED = "candidate-failed"
    CONFIGURATION_BLOCKED = "configuration-blocked"
    EXTERNAL_INCONCLUSIVE = "external-inconclusive"


class TerminalStatus(StrEnum):
    NO_UPDATE = "no-update"
    VALIDATED_CANDIDATE = "validated-candidate"
    REJECTED_RESOLUTION = "rejected-resolution"
    REJECTED_TESTS = "rejected-tests"
    REJECTED_SECURITY = "rejected-security"
    REJECTED_LIVE = "rejected-live"
    BLOCKED_BASELINE = "blocked-baseline"
    BLOCKED_CANARY_CONFIGURATION = "blocked-canary-configuration"
    INCONCLUSIVE_EXTERNAL = "inconclusive-external"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FrontierPolicy:
    python_version: str
    production_platform: str
    resolution_platforms: tuple[str, ...]
    cooldown_days: int
    allowed_source_builds: tuple[str, ...]
    license_denylist: tuple[str, ...]


@dataclass(frozen=True)
class DependencyEntry:
    scope: Literal["production", "development"]
    raw: str
    name: str

    @classmethod
    def from_raw(cls, scope: str, raw: str) -> DependencyEntry:
        if scope not in {"production", "development"}:
            raise ValueError(f"unsupported dependency scope: {scope}")
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise ValueError(f"invalid dependency requirement: {raw}") from exc
        return cls(
            scope=scope,
            raw=str(requirement),
            name=canonicalize_name(requirement.name),
        )


def load_project(path: Path) -> tuple[FrontierPolicy, tuple[DependencyEntry, ...]]:
    metadata = tomllib.loads(path.read_text(encoding="utf-8"))
    try:
        frontier = metadata["tool"]["dependency-frontier"]
        production = metadata["project"]["dependencies"]
        development = metadata["dependency-groups"]["dev"]
        policy = FrontierPolicy(
            python_version=str(frontier["python-version"]),
            production_platform=str(frontier["production-platform"]),
            resolution_platforms=tuple(str(item) for item in frontier["resolution-platforms"]),
            cooldown_days=int(frontier["cooldown-days"]),
            allowed_source_builds=tuple(canonicalize_name(item) for item in frontier["allowed-source-builds"]),
            license_denylist=tuple(str(item) for item in frontier["license-denylist"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("incomplete dependency frontier policy") from exc

    if not policy.python_version or not policy.production_platform or not policy.resolution_platforms:
        raise ValueError("dependency frontier platform policy must not be empty")
    if policy.production_platform not in policy.resolution_platforms:
        raise ValueError("production platform must be included in dependency frontier resolution platforms")
    if len(policy.resolution_platforms) != len(set(policy.resolution_platforms)):
        raise ValueError("dependency frontier resolution platforms must be unique")
    if policy.cooldown_days < 0:
        raise ValueError("dependency frontier cooldown must not be negative")

    dependencies = tuple(
        [DependencyEntry.from_raw("production", raw) for raw in production]
        + [DependencyEntry.from_raw("development", raw) for raw in development]
    )
    names = [item.name for item in dependencies]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate direct dependencies: {', '.join(duplicates)}")
    return policy, dependencies


def unconstrain_requirement(raw: str) -> str:
    requirement = Requirement(raw)
    if requirement.url is not None:
        raise ValueError(f"cannot unconstrain direct URL dependency: {requirement.name}")
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    marker = f"; {requirement.marker}" if requirement.marker is not None else ""
    return f"{requirement.name}{extras}{marker}"


def render_requirements_input(
    dependencies: tuple[DependencyEntry, ...] | list[DependencyEntry], *, unconstrained: bool
) -> str:
    rendered = [unconstrain_requirement(item.raw) if unconstrained else item.raw for item in dependencies]
    return "".join(f"{item}\n" for item in sorted(rendered, key=lambda item: canonicalize_name(Requirement(item).name)))


def classify_change(current: str, candidate: str) -> ChangeKind:
    old = Version(current)
    new = Version(candidate)
    if new == old:
        return ChangeKind.NONE
    if new < old:
        return ChangeKind.DOWNGRADE

    old_release = old.release + (0,) * (3 - len(old.release))
    new_release = new.release + (0,) * (3 - len(new.release))
    if new_release[0] != old_release[0]:
        return ChangeKind.MAJOR
    if new_release[1] != old_release[1]:
        return ChangeKind.MAJOR_RISK if old_release[0] == 0 else ChangeKind.MINOR
    return ChangeKind.PATCH


def is_schedule_due(current: date, epoch: date) -> bool:
    elapsed_days = (current - epoch).days
    return elapsed_days >= 0 and elapsed_days % 14 == 0


def normalize_compiled_requirements(content: str) -> tuple[str, ...]:
    requirements: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        line = line.removesuffix("\\").strip()
        if not line:
            continue
        requirements.append(str(Requirement(line)))
    return tuple(sorted(requirements, key=lambda item: (canonicalize_name(Requirement(item).name), item)))


def determine_terminal_status(
    *,
    has_update: bool,
    baseline_ok: bool,
    resolution_ok: bool,
    tests_ok: bool,
    security_ok: bool,
    live: LiveOutcome,
    cancelled: bool,
) -> TerminalStatus:
    if cancelled:
        return TerminalStatus.CANCELLED
    if not baseline_ok:
        return TerminalStatus.BLOCKED_BASELINE
    if not has_update:
        return TerminalStatus.NO_UPDATE
    if not resolution_ok:
        return TerminalStatus.REJECTED_RESOLUTION
    if not tests_ok:
        return TerminalStatus.REJECTED_TESTS
    if not security_ok:
        return TerminalStatus.REJECTED_SECURITY
    live_statuses = {
        LiveOutcome.PASSED: TerminalStatus.VALIDATED_CANDIDATE,
        LiveOutcome.CANDIDATE_FAILED: TerminalStatus.REJECTED_LIVE,
        LiveOutcome.CONFIGURATION_BLOCKED: TerminalStatus.BLOCKED_CANARY_CONFIGURATION,
        LiveOutcome.EXTERNAL_INCONCLUSIVE: TerminalStatus.INCONCLUSIVE_EXTERNAL,
        LiveOutcome.NOT_RUN: TerminalStatus.INCONCLUSIVE_EXTERNAL,
    }
    return live_statuses[live]


CommandRunner = Callable[[list[str], Path], None]


def build_compile_command(
    policy: FrontierPolicy,
    source: Path,
    output: Path,
    *,
    platform: str,
    cutoff: str,
) -> list[str]:
    if policy.allowed_source_builds:
        raise ValueError("selective source-build allowlists are not supported by this uv invocation")
    if platform not in policy.resolution_platforms:
        raise ValueError(f"platform is not covered by dependency frontier policy: {platform}")
    return [
        "uv",
        "pip",
        "compile",
        str(source),
        "--upgrade",
        "--resolution",
        "highest",
        "--prerelease",
        "disallow",
        "--exclude-newer",
        cutoff,
        "--python-version",
        policy.python_version,
        "--python-platform",
        platform,
        "--generate-hashes",
        "--no-header",
        "--no-annotate",
        "--no-build",
        "--output-file",
        str(output),
    ]


def _subprocess_runner(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False, text=True, capture_output=True, encoding="utf-8")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown resolver failure"
        raise RuntimeError(f"dependency resolver failed: {detail}")


def compile_candidate(
    name: str,
    source: Path,
    output_dir: Path,
    policy: FrontierPolicy,
    cutoff: str,
    *,
    platform: str,
    runner: CommandRunner = _subprocess_runner,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    first = output_dir / f"{name}.lock.txt"
    repeat = output_dir / f"{name}.repeat.lock.txt"
    runner(build_compile_command(policy, source, first, platform=platform, cutoff=cutoff), source.parent)
    runner(build_compile_command(policy, source, repeat, platform=platform, cutoff=cutoff), source.parent)
    first_normalized = normalize_compiled_requirements(first.read_text(encoding="utf-8"))
    repeat_normalized = normalize_compiled_requirements(repeat.read_text(encoding="utf-8"))
    repeat.unlink(missing_ok=True)
    if first_normalized != repeat_normalized:
        raise RuntimeError(f"non-deterministic dependency resolution for {name}")
    return first


def extract_locked_versions(content: str, direct_names: set[str]) -> dict[str, tuple[str, ...]]:
    versions: defaultdict[str, set[str]] = defaultdict(set)
    for item in normalize_compiled_requirements(content):
        requirement = Requirement(item)
        name = canonicalize_name(requirement.name)
        if name not in direct_names:
            continue
        for specifier in requirement.specifier:
            if specifier.operator == "==" and "*" not in specifier.version:
                versions[name].add(specifier.version)
    return {name: tuple(sorted(found, key=Version)) for name, found in sorted(versions.items())}


def _package_count(content: str) -> int:
    return len({canonicalize_name(Requirement(item).name) for item in normalize_compiled_requirements(content)})


def _highest_change(current: tuple[str, ...], candidate: tuple[str, ...]) -> ChangeKind:
    if not current or not candidate:
        return ChangeKind.NONE
    return classify_change(max(current, key=Version), max(candidate, key=Version))


def _classification(
    dependency: DependencyEntry,
    locked: tuple[str, ...],
    policy: tuple[str, ...],
    frontier: tuple[str, ...],
) -> str:
    requirement = Requirement(dependency.raw)
    policy_change = _highest_change(locked, policy)
    frontier_change = _highest_change(locked, frontier)
    upgrades = {ChangeKind.PATCH, ChangeKind.MINOR, ChangeKind.MAJOR, ChangeKind.MAJOR_RISK}
    if frontier_change in upgrades and any(Version(version) not in requirement.specifier for version in frontier):
        return "blocked-by-policy"
    if policy_change in upgrades:
        return "update-within-policy"
    if frontier_change in upgrades:
        return "frontier-update"
    if ChangeKind.DOWNGRADE in {policy_change, frontier_change}:
        return "cooldown-hold"
    return "current"


def write_audit_report(
    *,
    output_dir: Path,
    base_sha: str,
    cutoff: str,
    uv_version: str,
    python_version: str,
    platform: str,
    resolution_platforms: tuple[str, ...],
    input_hash: str,
    dependencies: tuple[DependencyEntry, ...],
    baseline_content: str,
    policy_content: str,
    frontier_content: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    direct_names = {dependency.name for dependency in dependencies}
    baseline_versions = extract_locked_versions(baseline_content, direct_names)
    policy_versions = extract_locked_versions(policy_content, direct_names)
    frontier_versions = extract_locked_versions(frontier_content, direct_names)

    rows: list[dict[str, object]] = []
    for dependency in sorted(dependencies, key=lambda item: item.name):
        locked = baseline_versions.get(dependency.name, ())
        policy = policy_versions.get(dependency.name, ())
        frontier = frontier_versions.get(dependency.name, ())
        classification = _classification(dependency, locked, policy, frontier)
        rows.append(
            {
                "name": dependency.name,
                "scope": dependency.scope,
                "constraint": dependency.raw,
                "locked": list(locked),
                "policy": list(policy),
                "frontier": list(frontier),
                "policy_change": _highest_change(locked, policy).value,
                "frontier_change": _highest_change(locked, frontier).value,
                "classification": classification,
                "actionable": classification in {"blocked-by-policy", "frontier-update", "update-within-policy"},
            }
        )

    report = {
        "schema_version": 1,
        "base_sha": base_sha,
        "cutoff": cutoff,
        "uv_version": uv_version,
        "python_version": python_version,
        "platform": platform,
        "resolution_platforms": list(resolution_platforms),
        "input_hash": input_hash,
        "dependencies": rows,
        "transitive": {
            "baseline_count": _package_count(baseline_content),
            "policy_count": _package_count(policy_content),
            "frontier_count": _package_count(frontier_content),
        },
    }
    json_path = output_dir / "dependency-frontier.json"
    markdown_path = output_dir / "dependency-frontier.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    markdown_lines = [
        "# Dependency Frontier Report",
        "",
        f"- Base SHA: `{base_sha}`",
        f"- Cutoff: `{cutoff}`",
        f"- uv: `{uv_version}`",
        f"- Python/platform: `{python_version}` / `{platform}`",
        f"- Resolved platforms: `{', '.join(resolution_platforms)}`",
        f"- Input hash: `{input_hash}`",
        "",
        "| Package | Scope | Constraint | Locked | Policy | Frontier | Classification |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for row in rows:
        locked_display = ", ".join(row["locked"]) or "—"
        policy_display = ", ".join(row["policy"]) or "—"
        frontier_display = ", ".join(row["frontier"]) or "—"
        markdown_lines.append(
            f"| {row['name']} | {row['scope']} | `{row['constraint']}` | {locked_display} | "
            f"{policy_display} | {frontier_display} | {row['classification']} |"
        )
    markdown_path.write_text("\n".join(markdown_lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def audit_project(
    *,
    pyproject_path: Path,
    output_dir: Path,
    cutoff: str,
    base_sha: str,
    uv_version: str,
    runner: CommandRunner = _subprocess_runner,
) -> Path:
    pyproject_path = pyproject_path.resolve()
    project_dir = pyproject_path.parent
    metadata_bytes = pyproject_path.read_bytes()
    metadata = tomllib.loads(metadata_bytes.decode("utf-8"))
    required_uv = str(metadata["tool"]["uv"]["required-version"])
    if not SpecifierSet(required_uv).contains(uv_version, prereleases=True):
        expected = required_uv.removeprefix("==")
        raise RuntimeError(f"project requires uv {expected} ({required_uv}), got {uv_version}")

    policy, dependencies = load_project(pyproject_path)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = output_dir / "baseline.lock.txt"
    policy_input = output_dir / "policy.in"
    frontier_input = output_dir / "frontier.in"
    policy_input.write_text(render_requirements_input(dependencies, unconstrained=False), encoding="utf-8")
    frontier_input.write_text(render_requirements_input(dependencies, unconstrained=True), encoding="utf-8")

    runner(
        [
            "uv",
            "export",
            "--locked",
            "--all-groups",
            "--no-hashes",
            "--no-header",
            "--no-annotate",
            "--no-emit-project",
            "--output-file",
            str(baseline_path),
        ],
        project_dir,
    )
    policy_contents: list[str] = []
    frontier_contents: list[str] = []
    for platform in policy.resolution_platforms:
        artifact_suffix = platform.replace("_", "-")
        policy_lock = compile_candidate(
            f"policy.{artifact_suffix}",
            policy_input,
            output_dir,
            policy,
            cutoff,
            platform=platform,
            runner=runner,
        )
        frontier_lock = compile_candidate(
            f"frontier.{artifact_suffix}",
            frontier_input,
            output_dir,
            policy,
            cutoff,
            platform=platform,
            runner=runner,
        )
        policy_contents.append(policy_lock.read_text(encoding="utf-8"))
        frontier_contents.append(frontier_lock.read_text(encoding="utf-8"))
    lock_bytes = (project_dir / "uv.lock").read_bytes()
    input_hash = hashlib.sha256(metadata_bytes + b"\0" + lock_bytes).hexdigest()
    json_path, _ = write_audit_report(
        output_dir=output_dir,
        base_sha=base_sha,
        cutoff=cutoff,
        uv_version=uv_version,
        python_version=policy.python_version,
        platform=policy.production_platform,
        resolution_platforms=policy.resolution_platforms,
        input_hash=input_hash,
        dependencies=dependencies,
        baseline_content=baseline_path.read_text(encoding="utf-8"),
        policy_content="\n".join(policy_contents),
        frontier_content="\n".join(frontier_contents),
    )
    return json_path


def _installed_uv_version() -> str:
    completed = subprocess.run(["uv", "--version"], check=True, capture_output=True, text=True, encoding="utf-8")
    parts = completed.stdout.strip().split()
    if len(parts) < 2 or parts[0] != "uv":
        raise RuntimeError(f"unexpected uv version output: {completed.stdout.strip()}")
    return parts[1]


def _git_sha(project_dir: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_dir, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.stdout.strip()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    due = subparsers.add_parser("schedule-due", help="Return success on a fixed fourteen-day audit date")
    due.add_argument("--date", default=date.today().isoformat())
    due.add_argument("--epoch", default="2026-09-07")

    audit = subparsers.add_parser("audit", help="Resolve policy and unconstrained dependency frontiers")
    audit.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    audit.add_argument("--output-dir", type=Path, default=Path("artifacts/dependency-frontier"))
    audit.add_argument("--cutoff")
    audit.add_argument("--base-sha")
    audit.add_argument("--uv-version")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "schedule-due":
        return 0 if is_schedule_due(date.fromisoformat(args.date), date.fromisoformat(args.epoch)) else 3

    policy, _ = load_project(args.project)
    cutoff = args.cutoff or (datetime.now(UTC) - timedelta(days=policy.cooldown_days)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    report = audit_project(
        pyproject_path=args.project,
        output_dir=args.output_dir,
        cutoff=cutoff,
        base_sha=args.base_sha or os.environ.get("GITHUB_SHA") or _git_sha(args.project.resolve().parent),
        uv_version=args.uv_version or _installed_uv_version(),
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

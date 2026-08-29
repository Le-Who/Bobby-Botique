"""Tests for the read-only dependency frontier model and CLI helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.dependency_frontier import (
    ChangeKind,
    DependencyEntry,
    FrontierPolicy,
    LiveOutcome,
    TerminalStatus,
    audit_project,
    build_compile_command,
    classify_change,
    compile_candidate,
    determine_terminal_status,
    extract_locked_versions,
    is_schedule_due,
    load_project,
    normalize_compiled_requirements,
    render_requirements_input,
    unconstrain_requirement,
    write_audit_report,
)

ROOT = Path(__file__).resolve().parents[1]


def test_load_project_reads_explicit_policy_and_dependency_scopes() -> None:
    policy, dependencies = load_project(ROOT / "pyproject.toml")

    assert policy == FrontierPolicy(
        python_version="3.14",
        production_platform="x86_64-manylinux_2_28",
        resolution_platforms=("x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"),
        cooldown_days=7,
        allowed_source_builds=(),
        license_denylist=(),
    )
    assert {item.scope for item in dependencies} == {"production", "development"}
    assert any(item.name == "python-telegram-bot" and item.scope == "production" for item in dependencies)
    assert any(item.name == "pytest" and item.scope == "development" for item in dependencies)


def test_load_project_requires_production_platform_to_be_resolved(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        dependencies = ["demo>=1"]

        [dependency-groups]
        dev = []

        [tool.dependency-frontier]
        python-version = "3.14"
        production-platform = "x86_64-manylinux_2_28"
        resolution-platforms = ["x86_64-pc-windows-msvc"]
        cooldown-days = 7
        allowed-source-builds = []
        license-denylist = []
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="production platform must be included"):
        load_project(pyproject)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("httpx>=0.25,<1", "httpx"),
        ("python-telegram-bot[job-queue]>=20.7,<23", "python-telegram-bot[job-queue]"),
        ("uvloop>=0.19,<1; sys_platform != 'win32'", 'uvloop; sys_platform != "win32"'),
        ("Pillow==12.0.0", "Pillow"),
    ],
)
def test_unconstrain_requirement_preserves_name_extras_and_marker(raw: str, expected: str) -> None:
    assert unconstrain_requirement(raw) == expected


def test_unconstrain_requirement_rejects_direct_urls() -> None:
    with pytest.raises(ValueError, match="direct URL"):
        unconstrain_requirement("demo @ https://example.invalid/demo.whl")


def test_render_requirements_input_is_stable_and_can_unconstrain() -> None:
    dependencies = (
        DependencyEntry.from_raw("production", "B>=2,<3"),
        DependencyEntry.from_raw("development", "a[fast]==1.2; python_version >= '3.14'"),
    )

    assert render_requirements_input(dependencies, unconstrained=False) == (
        'a[fast]==1.2; python_version >= "3.14"\nB<3,>=2\n'
    )
    assert render_requirements_input(dependencies, unconstrained=True) == ('a[fast]; python_version >= "3.14"\nB\n')


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        ("1.2.3", "1.2.4", ChangeKind.PATCH),
        ("1.2.3", "1.3.0", ChangeKind.MINOR),
        ("1.2.3", "2.0.0", ChangeKind.MAJOR),
        ("0.3.4", "0.4.0", ChangeKind.MAJOR_RISK),
        ("0.3.4", "0.3.5", ChangeKind.PATCH),
        ("1.0.0", "1.0.0", ChangeKind.NONE),
        ("2.0.0", "1.9.0", ChangeKind.DOWNGRADE),
    ],
)
def test_classify_change_is_conservative_for_pre_one_versions(
    current: str, candidate: str, expected: ChangeKind
) -> None:
    assert classify_change(current, candidate) is expected


def test_schedule_due_uses_fixed_fourteen_day_windows_across_year_boundary() -> None:
    epoch = date(2026, 12, 21)

    assert is_schedule_due(date(2026, 12, 21), epoch)
    assert not is_schedule_due(date(2026, 12, 28), epoch)
    assert is_schedule_due(date(2027, 1, 4), epoch)
    assert not is_schedule_due(date(2026, 12, 20), epoch)


def test_normalize_compiled_requirements_ignores_headers_comments_and_order() -> None:
    first = """
    # generated
    b==2.0 ; python_version >= '3.14'
        # via a
    a==1.0
    """
    second = """
    a==1.0
    b==2.0; python_version >= \"3.14\"
    """

    assert normalize_compiled_requirements(first) == normalize_compiled_requirements(second)
    assert normalize_compiled_requirements(first) == (
        "a==1.0",
        'b==2.0; python_version >= "3.14"',
    )


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"baseline_ok": False}, TerminalStatus.BLOCKED_BASELINE),
        ({"resolution_ok": False}, TerminalStatus.REJECTED_RESOLUTION),
        ({"tests_ok": False}, TerminalStatus.REJECTED_TESTS),
        ({"security_ok": False}, TerminalStatus.REJECTED_SECURITY),
        ({"live": LiveOutcome.CANDIDATE_FAILED}, TerminalStatus.REJECTED_LIVE),
        ({"live": LiveOutcome.CONFIGURATION_BLOCKED}, TerminalStatus.BLOCKED_CANARY_CONFIGURATION),
        ({"live": LiveOutcome.EXTERNAL_INCONCLUSIVE}, TerminalStatus.INCONCLUSIVE_EXTERNAL),
        ({"live": LiveOutcome.PASSED}, TerminalStatus.VALIDATED_CANDIDATE),
    ],
)
def test_terminal_status_never_treats_missing_or_failed_gate_as_success(
    kwargs: dict[str, object], expected: TerminalStatus
) -> None:
    defaults: dict[str, object] = {
        "has_update": True,
        "baseline_ok": True,
        "resolution_ok": True,
        "tests_ok": True,
        "security_ok": True,
        "live": LiveOutcome.NOT_RUN,
        "cancelled": False,
    }
    defaults.update(kwargs)

    assert determine_terminal_status(**defaults) is expected


def test_terminal_status_requires_live_gate_and_handles_no_update_or_cancellation() -> None:
    passing = {
        "baseline_ok": True,
        "resolution_ok": True,
        "tests_ok": True,
        "security_ok": True,
        "live": LiveOutcome.PASSED,
    }

    assert determine_terminal_status(has_update=False, cancelled=False, **passing) is TerminalStatus.NO_UPDATE
    assert determine_terminal_status(has_update=True, cancelled=True, **passing) is TerminalStatus.CANCELLED
    assert (
        determine_terminal_status(has_update=True, cancelled=False, **{**passing, "live": LiveOutcome.NOT_RUN})
        is TerminalStatus.INCONCLUSIVE_EXTERNAL
    )


def test_compile_command_freezes_resolver_inputs_and_disallows_source_builds(tmp_path: Path) -> None:
    policy = FrontierPolicy(
        python_version="3.14",
        production_platform="x86_64-manylinux_2_28",
        resolution_platforms=("x86_64-manylinux_2_28",),
        cooldown_days=7,
        allowed_source_builds=(),
        license_denylist=(),
    )
    source = tmp_path / "frontier.in"
    output = tmp_path / "frontier.lock.txt"

    command = build_compile_command(
        policy,
        source,
        output,
        platform="x86_64-manylinux_2_28",
        cutoff="2026-08-22T10:30:00Z",
    )

    assert command == [
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
        "2026-08-22T10:30:00Z",
        "--python-version",
        "3.14",
        "--python-platform",
        "x86_64-manylinux_2_28",
        "--generate-hashes",
        "--no-header",
        "--no-annotate",
        "--no-build",
        "--output-file",
        str(output),
    ]


def test_compile_candidate_runs_twice_and_rejects_non_deterministic_resolution(tmp_path: Path) -> None:
    policy = FrontierPolicy("3.14", "linux", ("linux",), 7, (), ())
    source = tmp_path / "frontier.in"
    source.write_text("demo\n", encoding="utf-8")
    outputs = ["demo==2.0\n", "demo==2.1\n"]
    commands: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> None:
        commands.append(command)
        output = Path(command[command.index("--output-file") + 1])
        output.write_text(outputs.pop(0), encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-deterministic"):
        compile_candidate(
            "frontier",
            source,
            tmp_path,
            policy,
            "2026-08-22T10:30:00Z",
            platform="linux",
            runner=runner,
        )

    assert len(commands) == 2


def test_compile_candidate_keeps_the_first_reproducible_lock(tmp_path: Path) -> None:
    policy = FrontierPolicy("3.14", "linux", ("linux",), 7, (), ())
    source = tmp_path / "policy.in"
    source.write_text("demo<2\n", encoding="utf-8")

    def runner(command: list[str], cwd: Path) -> None:
        output = Path(command[command.index("--output-file") + 1])
        output.write_text("demo==1.9\n", encoding="utf-8")

    result = compile_candidate(
        "policy",
        source,
        tmp_path,
        policy,
        "2026-08-22T10:30:00Z",
        platform="linux",
        runner=runner,
    )

    assert result.name == "policy.lock.txt"
    assert result.read_text(encoding="utf-8") == "demo==1.9\n"
    assert not (tmp_path / "policy.repeat.lock.txt").exists()


def test_report_never_presents_a_cooldown_downgrade_as_an_update(tmp_path: Path) -> None:
    dependencies = (DependencyEntry.from_raw("production", "demo>=1,<3"),)

    json_path, _ = write_audit_report(
        output_dir=tmp_path,
        base_sha="abc123",
        cutoff="2026-08-22T10:30:00Z",
        uv_version="0.12.6",
        python_version="3.14",
        platform="x86_64-manylinux_2_28",
        resolution_platforms=("x86_64-manylinux_2_28",),
        input_hash="deadbeef",
        dependencies=dependencies,
        baseline_content="demo==2.1\n",
        policy_content="demo==2.0\n",
        frontier_content="demo==2.0\n",
    )

    row = __import__("json").loads(json_path.read_text(encoding="utf-8"))["dependencies"][0]
    assert row["policy_change"] == "downgrade"
    assert row["frontier_change"] == "downgrade"
    assert row["classification"] == "cooldown-hold"
    assert row["actionable"] is False


def test_extract_locked_versions_handles_markers_and_multiple_versions() -> None:
    content = """
    demo==1.9 ; python_version < '3.14'
    demo==2.1 ; python_version >= '3.14'
    transitive==4.0
    """

    assert extract_locked_versions(content, {"demo"}) == {"demo": ("1.9", "2.1")}


def test_write_audit_report_distinguishes_policy_and_frontier_updates(tmp_path: Path) -> None:
    dependencies = (DependencyEntry.from_raw("production", "demo>=1,<2"),)

    json_path, markdown_path = write_audit_report(
        output_dir=tmp_path,
        base_sha="abc123",
        cutoff="2026-08-22T10:30:00Z",
        uv_version="0.12.6",
        python_version="3.14",
        platform="x86_64-manylinux_2_28",
        resolution_platforms=("x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"),
        input_hash="deadbeef",
        dependencies=dependencies,
        baseline_content="demo==1.5\ntransitive==3\n",
        policy_content="demo==1.9\ntransitive==4\n",
        frontier_content="demo==2.1\ntransitive==5\n",
    )

    report = __import__("json").loads(json_path.read_text(encoding="utf-8"))
    row = report["dependencies"][0]
    assert row == {
        "name": "demo",
        "scope": "production",
        "constraint": "demo<2,>=1",
        "locked": ["1.5"],
        "policy": ["1.9"],
        "frontier": ["2.1"],
        "policy_change": "minor",
        "frontier_change": "major",
        "classification": "blocked-by-policy",
        "actionable": True,
    }
    assert report["resolution_platforms"] == ["x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"]
    assert report["transitive"] == {
        "baseline_count": 2,
        "policy_count": 2,
        "frontier_count": 2,
    }
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "| demo | production | `demo<2,>=1` | 1.5 | 1.9 | 2.1 | blocked-by-policy |" in markdown
    assert "abc123" in markdown


def test_audit_project_is_read_only_and_writes_reproducible_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        """
        [project]
        name = "demo-app"
        version = "0.0.0"
        requires-python = ">=3.14,<3.15"
        dependencies = ["demo>=1,<2"]

        [dependency-groups]
        dev = ["pytest>=9,<10"]

        [tool.uv]
        package = false
        required-version = "==0.12.6"

        [tool.dependency-frontier]
        python-version = "3.14"
        production-platform = "x86_64-manylinux_2_28"
        resolution-platforms = ["x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"]
        cooldown-days = 7
        allowed-source-builds = []
        license-denylist = []
        """,
        encoding="utf-8",
    )
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    original_pyproject = pyproject.read_bytes()
    original_lock = (project / "uv.lock").read_bytes()
    output_dir = tmp_path / "artifacts"
    compile_counts = {"policy": 0, "frontier": 0}
    resolved_platforms: set[str] = set()

    def runner(command: list[str], cwd: Path) -> None:
        output = Path(command[command.index("--output-file") + 1])
        if command[1:3] == ["export", "--locked"]:
            output.write_text("demo==1.5\npytest==9.0\n", encoding="utf-8")
            return
        name = "frontier" if "frontier.in" in command[3] else "policy"
        compile_counts[name] += 1
        resolved_platforms.add(command[command.index("--python-platform") + 1])
        version = "2.1" if name == "frontier" else "1.9"
        output.write_text(f"demo=={version}\npytest==9.1\n", encoding="utf-8")

    report = audit_project(
        pyproject_path=pyproject,
        output_dir=output_dir,
        cutoff="2026-08-22T10:30:00Z",
        base_sha="abc123",
        uv_version="0.12.6",
        runner=runner,
    )

    assert report == output_dir / "dependency-frontier.json"
    assert compile_counts == {"policy": 4, "frontier": 4}
    assert resolved_platforms == {"x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"}
    assert (output_dir / "policy.in").read_text(encoding="utf-8") == "demo<2,>=1\npytest<10,>=9\n"
    assert (output_dir / "frontier.in").read_text(encoding="utf-8") == "demo\npytest\n"
    assert pyproject.read_bytes() == original_pyproject
    assert (project / "uv.lock").read_bytes() == original_lock


def test_audit_project_rejects_a_mismatched_uv_version_before_resolution(tmp_path: Path) -> None:
    pyproject = ROOT / "pyproject.toml"
    commands: list[list[str]] = []

    with pytest.raises(RuntimeError, match="requires uv 0.12.6"):
        audit_project(
            pyproject_path=pyproject,
            output_dir=tmp_path,
            cutoff="2026-08-22T10:30:00Z",
            base_sha="abc123",
            uv_version="0.11.0",
            runner=lambda command, cwd: commands.append(command),
        )

    assert commands == []

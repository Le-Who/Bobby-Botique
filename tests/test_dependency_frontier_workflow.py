"""Contracts for the scheduled dependency frontier and update policy."""

from pathlib import Path

WORKFLOW_PATH = Path(".github/workflows/dependency-frontier.yml")
DEPENDABOT_PATH = Path(".github/dependabot.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _dependabot() -> str:
    return DEPENDABOT_PATH.read_text(encoding="utf-8")


def test_frontier_workflow_has_an_exact_fourteen_day_gate_and_manual_override() -> None:
    workflow = _workflow()

    assert 'cron: "23 5 * * 1"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "schedule-due" in workflow
    assert "--epoch 2026-09-07" in workflow
    assert "EVENT_NAME: ${{ github.event_name }}" in workflow
    assert 'if [ "$EVENT_NAME" = "workflow_dispatch" ]' in workflow
    assert "steps.cadence.outputs.due == 'true'" in workflow


def test_frontier_discovery_is_read_only_pinned_and_secret_free() -> None:
    workflow = _workflow()
    audit_job = workflow.split("  audit:", 1)[1].split("  track-frontier:", 1)[0]

    assert "permissions:\n  contents: read" in workflow
    assert 'python -m pip install "uv==0.12.6"' in audit_job
    assert "uv sync --locked" in audit_job
    assert "scripts/dependency_frontier.py audit" in audit_job
    assert "secrets." not in audit_job
    assert "continue-on-error" not in audit_job
    assert "validated-candidate" not in workflow


def test_frontier_workflow_publishes_reports_and_tracks_one_durable_issue() -> None:
    workflow = _workflow()

    assert "actions/upload-artifact@v4" in workflow
    assert "dependency-frontier.json" in workflow
    assert "dependency-frontier.md" in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert "issues: write" in workflow
    assert "actions/github-script@v7" in workflow
    assert "Actionable dependency frontier" in workflow
    assert 'state: "closed"' in workflow


def test_dependabot_uses_uv_exact_fortnightly_cooldown_and_no_automerge() -> None:
    config = _dependabot()

    assert 'package-ecosystem: "uv"' in config
    assert 'directory: "/"' in config
    assert 'interval: "cron"' in config
    assert 'cronjob: "every 2 weeks"' in config
    assert "default-days: 7" in config
    assert "versioning-strategy: increase-if-necessary" in config
    assert "automerge" not in config.lower()


def test_dependabot_groups_only_post_one_minor_patch_updates() -> None:
    config = _dependabot()
    group = config.split("safe-minor-and-patch:", 1)[1]

    assert 'applies-to: "version-updates"' in group
    assert '- "minor"' in group
    assert '- "patch"' in group
    assert '- "major"' not in group
    for pre_one_dependency in ("asyncpg", "httpx", "hypercorn", "msgspec", "quart", "ruff", "tavily-python"):
        assert f'- "{pre_one_dependency}"' in group

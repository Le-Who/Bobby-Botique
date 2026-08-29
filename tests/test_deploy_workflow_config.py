from pathlib import Path

WORKFLOW_PATH = Path(".github/workflows/deploy.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_deploy_forwards_telegraph_privacy_opt_in() -> None:
    workflow = _workflow()

    assert "TELEGRAPH_PUBLICATION_ENABLED: ${{ secrets.TELEGRAPH_PUBLICATION_ENABLED || 'false' }}" in workflow
    assert "envs:" in workflow
    assert "TELEGRAPH_PUBLICATION_ENABLED" in workflow.split("envs:", 1)[1].splitlines()[0]
    assert '-e TELEGRAPH_PUBLICATION_ENABLED="$TELEGRAPH_PUBLICATION_ENABLED"' in workflow


def test_deploy_waits_for_successful_ci_on_active_branch() -> None:
    workflow = _workflow()

    assert "workflow_run:" in workflow
    assert 'workflows: ["CI"]' in workflow
    assert "branches: [vps_testai]" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow


def test_deploy_uses_exact_verified_commit_and_serializes_runs() -> None:
    workflow = _workflow()

    assert "concurrency:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in workflow
    assert "${{ github.event.workflow_run.head_sha }}" in workflow
    assert "IMAGE_TAG" in workflow
    assert "docker pull $REGISTRY/$REPO:$IMAGE_TAG" in workflow
    assert "docker pull $REGISTRY/$REPO:latest" not in workflow


def test_deploy_retries_build_without_hiding_second_failure() -> None:
    workflow = _workflow()
    initial_build = workflow.split("- name: Build and push Docker image", 1)[1].split(
        "- name: Retry build on transient failure", 1
    )[0]
    retry_build = workflow.split("- name: Retry build on transient failure", 1)[1].split(
        "- name: Prepare secrets for SSH", 1
    )[0]

    assert "continue-on-error: true" in initial_build
    assert "if: steps.build_image.outcome == 'failure'" in retry_build
    assert "continue-on-error: true" not in retry_build


def test_deploy_fails_closed_when_runtime_health_checks_expire() -> None:
    workflow = _workflow()

    assert "tg_api_is_ready()" in workflow
    assert "http://tg-api:8081/bot{token}/getMe" in workflow
    assert "docker exec tg-api wget" not in workflow
    assert "Existing Local Bot API container is unhealthy; recreating" in workflow
    assert "Telegram Bot API health check failed" in workflow
    assert 'curl -fsS "http://localhost:${PORT:-10000}/health"' in workflow
    assert "docker logs --tail 300 tg-bot" in workflow
    assert "Bot health check failed" in workflow

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_deploy_has_release_metadata_bounded_logs_and_no_raw_failure_tails():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert 'APP_RELEASE="$IMAGE_TAG"' in workflow
    assert 'LOG_FORMAT="$LOG_FORMAT"' in workflow
    assert "LOG_CONTENT_MODE: full" in workflow
    assert "--log-driver local" in workflow
    assert "--log-opt max-size=20m" in workflow
    assert "--log-opt max-file=5" in workflow
    assert "docker logs" not in workflow


def test_compose_uses_canonical_logging_configuration():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "LOG_FORMAT=json" in compose
    assert "LOG_CONTENT_MODE=full" in compose
    assert "LOG_JSON" not in compose
    assert 'max-size: "20m"' in compose
    assert 'max-file: "5"' in compose


def test_only_primary_bot_opts_into_observability_collection():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    candidate = re.search(
        r"if ! docker run -d \\\n\s+--name tg-bot \\\n(?P<arguments>.*?)"
        r"\s+\$REGISTRY/\$REPO:\$IMAGE_TAG; then",
        workflow,
        flags=re.DOTALL,
    )

    assert candidate is not None
    assert "--label com.gemaibot.logs=true" in candidate.group("arguments")
    assert workflow.count("--label com.gemaibot.logs=true") == 1


def test_deploy_installs_observability_stack_before_replacing_the_bot():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    bundle = workflow.index("OBSERVABILITY_BUNDLE_B64")
    stack_start = workflow.index('docker compose -f "$OBSERVABILITY_ROOT/compose.yml" up -d --remove-orphans')
    bot_start = workflow.index("if ! docker run -d")

    assert bundle < stack_start < bot_start
    assert "/opt/gemaibot-observability" in workflow
    assert 'openssl rand -base64 32 > "$GRAFANA_PASSWORD_FILE"' in workflow
    assert "secrets.GRAFANA_ADMIN_PASSWORD" not in workflow
    assert 'docker compose -f "$OBSERVABILITY_ROOT/compose.yml" config --quiet' in workflow


def test_deploy_waits_for_real_log_viewer_readiness():
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "up -d --remove-orphans --wait --wait-timeout 90" in workflow
    assert "http://127.0.0.1:3000/api/health" in workflow
    assert "http://alloy:12345/-/ready" in workflow
    assert "http://loki:3100/ready" in workflow

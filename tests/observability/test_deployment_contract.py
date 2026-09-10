from __future__ import annotations

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

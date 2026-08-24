from pathlib import Path


def test_deploy_forwards_telegraph_privacy_opt_in() -> None:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "TELEGRAPH_PUBLICATION_ENABLED: ${{ secrets.TELEGRAPH_PUBLICATION_ENABLED || 'false' }}" in workflow
    assert "envs:" in workflow
    assert "TELEGRAPH_PUBLICATION_ENABLED" in workflow.split("envs:", 1)[1].splitlines()[0]
    assert '-e TELEGRAPH_PUBLICATION_ENABLED="$TELEGRAPH_PUBLICATION_ENABLED"' in workflow

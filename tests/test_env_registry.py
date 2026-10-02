"""Static config checks must inspect actual syntax, never a live environment."""

import json

from scripts.check_env_registry import inventory, main


def test_inventory_ignores_comments_and_distinguishes_docker_forwarding(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/config.py").write_text(
        'import os\n# os.getenv("FAKE")\nx = os.getenv("ACTUAL", "safe")\ny = _load_single_model("MODEL", "chosen")\n',
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/deploy.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        'env:\n  WORKFLOW_ONLY: ${{ secrets.WORKFLOW_ONLY }}\nrun: docker run -e ACTUAL="$ACTUAL" image\n',
        encoding="utf-8",
    )
    rows = inventory(tmp_path)
    assert set(rows) == {"ACTUAL", "MODEL"}
    assert rows["ACTUAL"]["deploy"] is True
    assert rows["MODEL"]["deploy"] is False


def test_registry_detects_new_readers_and_does_not_consult_environment(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    source = tmp_path / "app/config.py"
    source.write_text('import os\nx = os.getenv("KEY")\n', encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(inventory(tmp_path)), encoding="utf-8")
    monkeypatch.setenv("KEY", "private-value-must-not-be-read")
    assert main(["--repo", str(tmp_path), "--registry", "registry.json"]) == 0
    source.write_text('import os\nx = os.getenv("NEW_KEY")\n', encoding="utf-8")
    assert main(["--repo", str(tmp_path), "--registry", "registry.json"]) == 1

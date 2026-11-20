import ast
from pathlib import Path

import yaml


def _assert_runtime_release_order(source: str) -> None:
    tree = ast.parse(source)
    startup = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_bot_with_retry"
    )
    awaited = [node for node in ast.walk(startup) if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)]
    release = [
        node
        for node in awaited
        if isinstance(node.value.func, ast.Name) and node.value.func.id == "release_cloud_bot_api_session"
    ]
    webhook = [node for node in awaited if ast.unparse(node.value.func) == "application.bot.set_webhook"]
    assert len(release) == 1, "Startup must await cloud Bot API release"
    assert len(webhook) == 1, "Startup must await webhook registration"
    assert release[0].lineno < webhook[0].lineno, "Cloud Bot API release must precede webhook registration"


def test_deploy_releases_cloud_bot_api_without_one_time_flag() -> None:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "/opt/tg-local-api-migrated" not in workflow

    steps = yaml.safe_load(workflow)["jobs"]["build-and-deploy"]["steps"]
    ssh_step = next(step for step in steps if step.get("uses", "").startswith("appleboy/ssh-action@"))
    script = ssh_step["with"]["script"]
    release = script.index("python /app/scripts/release_cloud_bot_api.py")
    assert release < script.index("if ! tg_api_is_ready; then")
    assert release < script.index("--name tg-api ")


def test_runtime_releases_cloud_bot_api_before_setting_local_webhook() -> None:
    source = Path("bot.py").read_text(encoding="utf-8")

    _assert_runtime_release_order(source)

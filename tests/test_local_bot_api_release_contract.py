import ast
from pathlib import Path


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
    assert "python /app/scripts/release_cloud_bot_api.py" in workflow
    assert workflow.index("python /app/scripts/release_cloud_bot_api.py") < workflow.index(
        "Local Telegram Bot API Server"
    )


def test_runtime_releases_cloud_bot_api_before_setting_local_webhook() -> None:
    source = Path("bot.py").read_text(encoding="utf-8")

    _assert_runtime_release_order(source)

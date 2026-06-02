from pathlib import Path


def test_deploy_releases_cloud_bot_api_without_one_time_flag() -> None:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "/opt/tg-local-api-migrated" not in workflow
    assert "python /app/scripts/release_cloud_bot_api.py" in workflow
    assert workflow.index("python /app/scripts/release_cloud_bot_api.py") < workflow.index(
        "Local Telegram Bot API Server"
    )


def test_runtime_releases_cloud_bot_api_before_setting_local_webhook() -> None:
    source = Path("bot.py").read_text(encoding="utf-8")

    assert "release_cloud_bot_api_session" in source
    assert source.index("release_cloud_bot_api_session") < source.index("await application.bot.set_webhook")

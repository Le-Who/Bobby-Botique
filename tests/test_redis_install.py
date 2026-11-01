"""Installation must preserve credentials and reject another project's resources."""

import importlib.util
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("redis_install", "ops/redis/install.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_install_rejects_foreign_resource_before_mutation():
    installer = module()
    with pytest.raises(RuntimeError, match="ownership"):
        installer.validate_resource({"Labels": {"com.docker.compose.project": "other"}}, "network")
    installer.validate_resource({"Labels": {"com.gemaibot.redis": "true"}}, "network")


def test_credentials_are_stable_and_missing_secret_never_rotates_existing_data(tmp_path):
    installer = module()
    installer.prepare_credentials(tmp_path, existing_data=False)
    original = (tmp_path / "bot.env").read_bytes()
    installer.prepare_credentials(tmp_path, existing_data=True)
    assert (tmp_path / "bot.env").read_bytes() == original
    (tmp_path / "secrets" / "app_password").unlink()
    with pytest.raises(RuntimeError, match="credentials"):
        installer.prepare_credentials(tmp_path, existing_data=True)
    assert (tmp_path / "bot.env").read_bytes() == original


def test_install_requires_completed_migration(tmp_path):
    installer = module()
    with pytest.raises(RuntimeError, match="migration"):
        installer.require_migration(tmp_path)
    (tmp_path / "migration-complete").write_text("verified\n", encoding="utf-8")
    installer.require_migration(tmp_path)


def test_completed_migration_cannot_recreate_an_empty_volume():
    installer = module()
    with pytest.raises(RuntimeError, match="restore required"):
        installer.validate_ready_volume(None)

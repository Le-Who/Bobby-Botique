import pytest

from scripts import natal_maintenance


def test_natal_maintenance_uses_configured_ttl(monkeypatch, capsys):
    calls = []

    async def fake_check_storage_ready():
        calls.append(("check", None))

    async def fake_purge_expired_reports(ttl_days: int):
        calls.append(("purge", ttl_days))
        return 3

    class FakeSettings:
        NATAL_REPORT_TTL_DAYS = 90

    monkeypatch.setattr("app.natal.storage.check_storage_ready", fake_check_storage_ready)
    monkeypatch.setattr("app.natal.storage.purge_expired_reports", fake_purge_expired_reports)
    monkeypatch.setattr("app.config.settings", FakeSettings())

    assert natal_maintenance.main([]) == 0

    assert calls == [("check", None), ("purge", 90)]
    assert "deleted=3" in capsys.readouterr().out


def test_natal_maintenance_accepts_ttl_override(monkeypatch, capsys):
    calls = []

    async def fake_check_storage_ready():
        calls.append(("check", None))

    async def fake_purge_expired_reports(ttl_days: int):
        calls.append(("purge", ttl_days))
        return 1

    monkeypatch.setattr("app.natal.storage.check_storage_ready", fake_check_storage_ready)
    monkeypatch.setattr("app.natal.storage.purge_expired_reports", fake_purge_expired_reports)

    assert natal_maintenance.main(["--ttl-days", "30"]) == 0

    assert calls == [("check", None), ("purge", 30)]
    assert "ttl_days=30" in capsys.readouterr().out

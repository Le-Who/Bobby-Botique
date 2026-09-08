import sys
from types import SimpleNamespace

import pytest

from app.config import settings
from scripts import natal_smoke


@pytest.mark.parametrize("user_args, expected", [([], 321), (["--user-id", "123"], 123)])
def test_standalone_smoke_uses_admin_unless_user_is_explicit(monkeypatch, user_args, expected, capsys):
    monkeypatch.setattr(settings, "ADMIN_ID", 321)
    monkeypatch.setattr(sys, "argv", ["natal_smoke.py", "--webhook-url", "https://bot.example.com", *user_args])
    captured = {}

    async def smoke(webhook_url, user_id, chat_id):
        captured["user_id"] = user_id
        return SimpleNamespace(
            report_id="test-report",
            hosted_url="https://bot.example.com/reports/natal/test-report",
            telegraph_url=None,
            planet_count=10,
            section_count=3,
            hosted_html_contains_svg=True,
            hosted_html_contains_sections=True,
        )

    monkeypatch.setattr("app.natal.smoke.run_natal_smoke", smoke)
    assert natal_smoke.main() == 0
    assert captured["user_id"] == expected
    assert "OK report_id=test-report" in capsys.readouterr().out

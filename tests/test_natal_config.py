from app.config import load_settings


def test_natal_privacy_defaults_do_not_send_raw_birth_data(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("GEMINI_API_KEYS", "k1")
    monkeypatch.setenv("TAVILY_API_KEYS", "k2")

    settings = load_settings()

    assert settings.NATAL_REPORTS_ENABLED is False
    assert settings.NATAL_REPORT_TTL_DAYS == 365
    assert settings.NATAL_GEOCODER_PROVIDER == "local"
    assert settings.NATAL_SEND_RAW_BIRTH_DATA_TO_LLM is False

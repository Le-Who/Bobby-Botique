"""Read shipped Daily scripts for the narrow executable Node transport probes."""

from pathlib import Path


def read_admin_daily_sources() -> str:
    root = Path(__file__).resolve().parents[1] / "app"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            root / "templates/admin_daily.html",
            root / "static/js/daily-broadcast.js",
            root / "static/js/daily-croc-editor.js",
            root / "static/js/daily-trivia-editor.js",
        ]
    )

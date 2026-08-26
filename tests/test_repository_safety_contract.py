"""Static safety contracts for repository-level maintenance entry points."""

from pathlib import Path


def test_unsafe_clear_db_script_and_lint_exception_are_absent() -> None:
    assert not Path("clear_db.py").exists()

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"clear_db.py"' not in pyproject

"""Exercise the documentation guard as a CLI without importing the application."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_encoding.py"


def run_check(script, *paths, cwd=None):
    return subprocess.run(
        [sys.executable, str(script), *map(str, paths)],
        cwd=cwd,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


def test_accepts_unicode_punctuation_emoji_and_normal_line_endings(tmp_path):
    doc = tmp_path / "guide.md"
    doc.write_bytes("# Привет ⚙️ 👩‍💻\r\n\t‘Текст’ — déjà vu → OK\n".encode())
    assert run_check(SCRIPT, doc).returncode == 0


@pytest.mark.parametrize(
    "bad", [b"\xff", b"before\x07after", b"before\x0cafter", b"before\x7fafter", "bad\u0085text".encode()]
)
def test_rejects_invalid_utf8_and_control_characters_in_any_document(tmp_path, bad):
    doc = tmp_path / "guide.md"
    doc.write_bytes(b"# Heading\n" + bad)
    result = run_check(SCRIPT, doc)
    assert result.returncode == 1
    assert "guide.md" in result.stdout
    assert "Traceback" not in result.stderr
    assert doc.read_bytes() == b"# Heading\n" + bad


def test_preserves_known_mojibake_detection(tmp_path):
    doc = tmp_path / "guide.md"
    doc.write_bytes(b"broken " + bytes([0xC3, 0xB0, 0xC2, 0x9F]))
    result = run_check(SCRIPT, doc)
    assert result.returncode == 1
    assert "mojibake" in result.stdout.lower()


def test_missing_explicit_file_fails_closed(tmp_path):
    result = run_check(SCRIPT, tmp_path / "missing.md")
    assert result.returncode == 1
    assert "missing.md" in result.stdout


def test_reports_all_bad_files_with_ascii_terminal_and_unicode_filename(tmp_path):
    docs = [tmp_path / "память.md", tmp_path / "other.md"]
    for doc in docs:
        doc.write_bytes(b"bad\x07data")
    result = run_check(SCRIPT, *docs)
    assert result.returncode == 1
    assert "other.md" in result.stdout
    assert "Traceback" not in result.stderr


def test_default_discovery_checks_new_and_tracked_docs_but_not_ignored_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "check_encoding.py"
    shutil.copyfile(SCRIPT, script)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Valid\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "bad.md").write_bytes(b"\xff")
    (tmp_path / "docs").mkdir()
    new = tmp_path / "docs" / "new.MD"
    new.write_bytes(b"bad\x07data")
    result = run_check(script, cwd=tmp_path / "docs")
    assert result.returncode == 1
    assert "new.MD" in result.stdout
    new.write_text("# Fixed\n", encoding="utf-8")
    assert run_check(script, cwd=tmp_path / "docs").returncode == 0
    (tmp_path / "README.md").write_bytes(b"bad\xff")
    assert run_check(script).returncode == 1


def test_discovery_failure_is_not_a_clean_result(tmp_path):
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "check_encoding.py"
    shutil.copyfile(SCRIPT, script)
    result = run_check(script)
    assert result.returncode != 0

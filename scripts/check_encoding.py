#!/usr/bin/env python3
"""Read-only UTF-8/control-character guard for repository Markdown.

With filenames, check exactly those files (pre-commit passes staged filenames).
Without filenames, discover tracked and non-ignored new Markdown via Git.
Install through pre-commit install; do not copy this script into .git/hooks.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Retain the known corruption signatures used by the original guard.
BROKEN_PATTERNS = (
    bytes([0xC3, 0xA2, 0x20]),
    bytes([0xC3, 0xA2, 0xC2, 0x8C]),
    bytes([0xC3, 0xA2, 0xC2, 0x9C]),
    bytes([0xC3, 0xA2, 0xC2, 0x86]),
    bytes([0xC3, 0xB0, 0xC2, 0x9F]),
)


def check_file(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"{path}: cannot read file ({exc.strerror})"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{path}: invalid UTF-8 at byte {exc.start}"]

    errors = []
    for pattern in BROKEN_PATTERNS:
        if pattern in data:
            errors.append(f"{path}: known mojibake pattern {pattern.hex()}")
    # Split only on LF: str.splitlines() would hide form feed and C1 controls.
    for line_number, line in enumerate(text.split("\n"), 1):
        controls = sorted(
            {ord(char) for char in line if (ord(char) < 32 and char not in "\t\r") or 127 <= ord(char) <= 159}
        )
        if controls:
            codes = ", ".join(f"U+{code:04X}" for code in controls)
            errors.append(f"{path}:{line_number}: unexpected control characters {codes}")
    return errors


def discover_documents(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    paths = {root / name for name in result.stdout.decode("utf-8").split("\0") if name}
    # Deleted tracked files have no working-tree content to validate.
    return sorted(path for path in paths if path.suffix.lower() == ".md" and path.exists())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    # Diagnostics must work even on an ASCII/cp1251 terminal with Unicode paths.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        paths = args.files or discover_documents(Path(__file__).resolve().parents[1])
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: cannot discover Markdown files ({type(exc).__name__})")
        return 2
    errors = [error for path in paths for error in check_file(path)]
    if errors:
        print("FAIL: documentation encoding integrity")
        for error in errors:
            print(error)
        print("Restore the affected text from a verified source; no files were modified.")
        return 1
    print(f"PASS: encoding integrity ({len(paths)} Markdown files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

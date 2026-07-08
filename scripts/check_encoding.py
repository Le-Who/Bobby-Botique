#!/usr/bin/env python3
"""
Pre-commit hook: detects UTF-8 mojibake in documentation files.

Blocks commit if any of the known Latin-1-reinterpretation patterns are found
in README.md or CHANGELOG.md.

TYPE-A: C3 A2 followed by control byte (broken â + continuation)
        = UTF-8 multi-byte sequence had bytes misread as Latin-1 codepoints
TYPE-B: C3 B0 + C2 9F (broken ð + control)
        = F0 9F emoji leader bytes misread as Latin-1

Install: copy or symlink to .git/hooks/pre-commit and chmod +x
"""

import sys
from pathlib import Path

# Patterns that indicate the file was written with wrong encoding
BROKEN_PATTERNS: list[bytes] = [
    bytes([0xC3, 0xA2, 0x20]),  # â + space  (broken E2 xx sequence)
    bytes([0xC3, 0xA2, 0xC2, 0x8C]),  # â + U+008C (alt broken gear)
    bytes([0xC3, 0xA2, 0xC2, 0x9C]),  # â + U+009C
    bytes([0xC3, 0xA2, 0xC2, 0x86]),  # â + U+0086 (broken arrow →)
    bytes([0xC3, 0xB0, 0xC2, 0x9F]),  # ð + U+009F (broken F0 9F emoji)
]

DOCS = ["README.md", "CHANGELOG.md"]


def check_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = path.read_bytes()
    errors = []
    for pat in BROKEN_PATTERNS:
        count = data.count(pat)
        if count:
            errors.append(
                f"  {path.name}: found broken pattern {pat.hex()} "
                f"({count} occurrences) — likely Latin-1 reinterpretation of UTF-8"
            )
    return errors


root = Path(__file__).parent.parent  # .git/hooks/pre-commit → repo root
all_errors: list[str] = []
for doc in DOCS:
    all_errors.extend(check_file(root / doc))

if all_errors:
    print("❌ PRE-COMMIT BLOCKED: UTF-8 mojibake detected in docs:")
    for e in all_errors:
        print(e)
    print()
    print("Fix: run  python _fix_encoding.py  in the project root,")
    print("     then re-stage the file and retry the commit.")
    print()
    print("Root cause: agent opened file without encoding='utf-8'.")
    print("Always use: open(file, encoding='utf-8') for README.md / CHANGELOG.md")
    sys.exit(1)

sys.exit(0)

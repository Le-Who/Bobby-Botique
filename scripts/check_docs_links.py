"""Offline relative Markdown link/heading checks for current repository guides.

Historical plans/reports are checked only with --include-historical. No network
or application imports. Covers inline/image/reference links, ATX/Setext headings
and explicit HTML anchors; not a complete CommonMark renderer.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from scripts.check_encoding import discover_documents
except ModuleNotFoundError:
    from check_encoding import discover_documents

DEST = r"(<[^>]+>|(?:[^\s()]|\([^()]*\))+)"
INLINE = re.compile(r"!?\[[^\]]*\]\(" + DEST + r"(?:\s+[\"\'][^\"\']*[\"\'])?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*" + DEST)


def prose(path: Path) -> list[str]:
    lines = []
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, rest = marker.groups()
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = ""
            lines.append("")
        else:
            lines.append("" if fence else line)
    return lines


def headings(path: Path) -> set[str]:
    anchors: set[str] = set()
    lines = prose(path)
    for index, line in enumerate(lines):
        anchors.update(re.findall(r'<[^>]+\b(?:id|name)=["\']([^"\']+)["\']', line))
        match = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        title = match.group(1) if match else None
        if title is None and index and re.fullmatch(r"\s{0,3}(?:=+|-+)\s*", line) and lines[index - 1].strip():
            title = lines[index - 1].strip()
        if title is None:
            continue
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = re.sub(r"<[^>]*>", "", title).lower()
        slug = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
        unique, suffix = slug, 0
        while unique in anchors:
            suffix += 1
            unique = f"{slug}-{suffix}"
        anchors.add(unique)
    return anchors


def check_document(path: Path, root: Path) -> list[str]:
    errors = []
    cache: dict[Path, set[str]] = {}
    for number, raw in enumerate(prose(path), 1):
        line = re.sub(r"(`+).*?\1", "", raw)
        targets = INLINE.findall(line)
        reference = REFERENCE.match(line)
        if reference:
            targets.append(reference.group(1))
        for target in targets:
            target = target.removeprefix("<").removesuffix(">")
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            name, anchor = unquote(url.path), unquote(url.fragment)
            dest = ((root / name.lstrip("/")) if name.startswith("/") else path.parent / name) if name else path
            dest = dest.resolve()
            problem = None
            if not dest.exists():
                problem = "missing target"
            elif anchor and dest.suffix.lower() == ".md":
                if dest not in cache:
                    cache[dest] = headings(dest)
                if anchor not in cache[dest]:
                    problem = "missing anchor"
            if problem:
                errors.append(f"{path.relative_to(root)}:{number}: {problem}: {target}")
    return errors


def historical(path: Path) -> bool:
    return (
        any(part in {"superpowers", ".jules"} for part in path.parts)
        or path.name in {"CHANGELOG.md", "MAINTAINER_QUEUE.md"}
        or bool(re.search(r"\d{4}-\d{2}-\d{2}", path.name))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-historical", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    paths = [
        p
        for p in discover_documents(root)
        if p.relative_to(root).parts[0] != "revisions"
        and (args.include_historical or not historical(p.relative_to(root)))
    ]
    errors = [error for path in paths for error in check_document(path, root)]
    for error in errors:
        print(error)
    print(f"{'FAIL' if errors else 'PASS'}: {len(paths)} Markdown documents; {len(errors)} link errors")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

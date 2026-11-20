"""Reject new prose comments in first-party code and configuration."""

import argparse
import io
import subprocess
import sys
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from tokenize import TokenError

from source_comments import SourceComment, prose_comments, source_format
from yaml import YAMLError

REPORT_LIMIT = 50


def git(root: Path, *args: str, data: bytes | None = None, check: bool = True) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=data, capture_output=True, check=check, timeout=60
    ).stdout


def names(data: bytes) -> list[str]:
    return data.removesuffix(b"\0").decode("utf-8").split("\0") if data else []


def working_tree(root: Path, paths: set[str]) -> dict[str, bytes]:
    contents = {}
    for name in sorted(paths):
        path = root / name
        if source_format(name) and path.is_file() and path.resolve() == path:
            contents[name] = path.read_bytes()
    return contents


def snapshot(root: Path, revision: str, paths: set[str] | None = None) -> dict[str, bytes]:
    objects = {}
    for entry in names(git(root, "ls-tree", "-r", "-z", revision)):
        metadata, name = entry.split("\t", 1)
        mode, _, oid = metadata.split()
        if mode in {"100644", "100755"} and (paths is None or name in paths) and source_format(name):
            objects[name] = oid.encode("ascii")
    raw = git(root, "cat-file", "--batch", data=b"".join(oid + b"\n" for oid in objects.values()))
    stream = io.BytesIO(raw)
    contents = {}
    for name, expected in objects.items():
        oid, kind, size = stream.readline().split()
        if oid != expected or kind != b"blob":
            raise ValueError("invalid Git blob response")
        contents[name] = stream.read(int(size))
        if stream.read(1) != b"\n":
            raise ValueError("truncated Git blob response")
    return contents


def resolve_base(root: Path, ref: str) -> str:
    return git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()


def baselines(root: Path, ref: str | None) -> list[str]:
    if ref is not None:
        return [resolve_base(root, ref)]
    head = resolve_base(root, "HEAD")
    upstream = git(root, "rev-parse", "--verify", "@{upstream}^{commit}", check=False)
    if upstream:
        upstream_id = upstream.decode("ascii").strip()
        base = git(root, "merge-base", head, upstream_id).decode("ascii").strip()
        return list(dict.fromkeys((head, base)))
    return [head]


def scan(name: str, content: bytes) -> tuple[str, list[SourceComment]]:
    text = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return text, list(prose_comments(text, source_format(name)))


def new_comments(
    root: Path, current: dict[str, bytes], revisions: list[str]
) -> tuple[list[tuple[str, int]], list[str]]:
    previous = [snapshot(root, revision, set(current)) for revision in revisions]
    findings = []
    errors = []
    for name, content in current.items():
        if all(content == baseline.get(name) for baseline in previous):
            continue
        try:
            text, candidates = scan(name, content)
            if not candidates:
                continue
            allowances = []
            for baseline in previous:
                if name in baseline:
                    _, comments = scan(name, baseline[name])
                    allowances.append(Counter(comment.text for comment in comments))
                else:
                    allowances.append(Counter())
            allowed = allowances[0]
            for allowance in allowances[1:]:
                allowed &= allowance
        except (UnicodeError, TokenError, SyntaxError, YAMLError) as exc:
            errors.append(f"{name}: cannot tokenize source ({type(exc).__name__})")
            continue
        for comment in candidates:
            if allowed[comment.text]:
                allowed[comment.text] -= 1
            else:
                findings.append((name, text.count("\n", 0, comment.start) + 1))
    return findings, errors


def physical_lines(text: str, comments: list[SourceComment]) -> tuple[int, int]:
    starts = [0]
    starts.extend(index + 1 for index, char in enumerate(text) if char == "\n")
    touched = set()
    parts = []
    previous = 0
    for comment in comments:
        start = max(previous, comment.start)
        end = max(start, comment.end)
        if end > start:
            touched.update(range(bisect_right(starts, start) - 1, bisect_right(starts, end - 1)))
            parts.extend((text[previous:start], "\n" * text.count("\n", start, end)))
            previous = end
    remainder = "".join(parts) + text[previous:]
    lines = remainder.split("\n")
    return len(touched), sum(not lines[index].strip() for index in touched)


def inventory(contents: dict[str, bytes]) -> int:
    totals: Counter = Counter()
    errors = []
    for name, content in contents.items():
        try:
            text, comments = scan(name, content)
        except (UnicodeError, TokenError, SyntaxError, YAMLError) as exc:
            errors.append(f"{name}: cannot tokenize source ({type(exc).__name__})")
            continue
        touched, removable = physical_lines(text, comments)
        totals.update(files=1, comments=len(comments), touched=touched, removable=removable)
    if errors:
        for error in errors[:REPORT_LIMIT]:
            print(error)
        print(f"FAIL: {len(errors)} source files could not be inventoried.")
        return 2
    print(f"Scanned {totals['files']} source files.")
    print(f"Prose comments: {totals['comments']}")
    print(f"Physical lines containing prose comments: {totals['touched']}")
    print(f"Physical lines removable with the comments: {totals['removable']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", metavar="REV", help="compare against a specific Git revision")
    parser.add_argument(
        "--inventory", action="store_true", help="count existing comments in the working tree or --base"
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="backslashreplace")
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").rstrip("\r\n")).resolve()
        if args.inventory:
            if args.base:
                return inventory(snapshot(root, resolve_base(root, args.base)))
            paths = set(names(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")))
            return inventory(working_tree(root, paths))
        revisions = baselines(root, args.base)
        paths = set(names(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
        for revision in revisions:
            paths.update(names(git(root, "diff", "--name-only", "--no-renames", "-z", revision, "--")))
        findings, errors = new_comments(root, working_tree(root, paths), revisions)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            f"FAIL: cannot read repository or baseline ({type(exc).__name__}). Check that the base commit is available."
        )
        return 2

    for error in errors[:REPORT_LIMIT]:
        print(error)
    for name, line in findings[:REPORT_LIMIT]:
        print(f"{name}:{line}: new prose comment")
    if errors or findings:
        print(f"FAIL: {len(findings)} new prose comments and {len(errors)} source scan errors.")
        return 2 if errors else 1
    print("PASS: no new prose comments")
    return 0


if __name__ == "__main__":
    sys.exit(main())

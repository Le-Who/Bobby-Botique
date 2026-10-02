"""Compare a source-derived environment inventory without importing the bot.

The registry records literal readers and explicit docker forwarding, not live
values, requiredness, reload guarantees or deployment success. Dynamic names
and shell/YAML indirection need manual review. --write refreshes the snapshot.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
HELPERS = {
    "_load_and_clean_keys",
    "_load_int_env",
    "_load_single_model",
    "_load_gemini_role_model",
    "_load_available_models",
}


def readers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        key = None
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if func in {"os.getenv", "os.environ.get", *HELPERS} and node.args:
                key = node.args[0]
            # LoggingSettings reads an explicitly supplied environment mapping.
            if path.as_posix().endswith("app/observability/config.py"):
                if func == "source.get" and node.args:
                    key = node.args[0]
                elif func == "_positive_int" and len(node.args) > 1:
                    key = node.args[1]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            if isinstance(node.ctx, ast.Load):
                key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and NAME.fullmatch(key.value):
            found.add(key.value)
    return found


def inventory(repo: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    files = sorted((repo / "app").rglob("*.py"))
    if (repo / "bot.py").exists():
        files.append(repo / "bot.py")
    for path in files:
        for name in sorted(readers(path)):
            result.setdefault(name, {"readers": [], "deploy": False, "compose": False})["readers"].append(
                path.relative_to(repo).as_posix()
            )
    for relative, field, pattern in (
        (".github/workflows/deploy.yml", "deploy", r"(?:-e\s+|--env[=\s]+)[\"']?([A-Z][A-Z0-9_]*)="),
        ("docker-compose.yml", "compose", r"^\s*-\s*([A-Z][A-Z0-9_]*)="),
    ):
        path = repo / relative
        if path.exists():
            text = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
            )
            for name in re.findall(pattern, text, re.MULTILINE):
                result.setdefault(name, {"readers": [], "deploy": False, "compose": False})[field] = True
    return dict(sorted(result.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--registry", type=Path, default=Path("docs/config-registry.json"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    path = args.registry if args.registry.is_absolute() else args.repo / args.registry
    actual = inventory(args.repo)
    if args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {len(actual)} entries; review source changes before accepting the snapshot.")
        return 0
    expected = json.loads(path.read_text(encoding="utf-8"))
    changed = sorted(name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name))
    for name in changed:
        print(f"Inventory drift: {name}")
    print(f"{'FAIL' if changed else 'PASS'}: {len(actual)} environment names; {len(changed)} changes")
    return int(bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        (
            "values.py",
            '#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"""# documentation"""\n'
            'data = "# literal"  # noqa: F401\ntext = f"""{value # interpolation\n}"""\n'
            'data = "# literal"  # noqa: F401 -- new explanation\n',
            ["# interpolation", "# noqa: F401 -- new explanation"],
        ),
        (
            "values.js",
            'const regex = /[//]/; const url = "https://example.test"; // explanation\n'
            "const label = `// literal ${ /* interpolation */ 1 }`;\n",
            ["// explanation", "/* interpolation */"],
        ),
        (
            "page.html",
            '<!-- markup {{ something }} spanning a template --><button title="<!-- data -->">{{ name }}</button>\n'
            "<style>/* theme */ header { color: #fff; }</style>\n"
            '<script>const value = "// literal"; // script\n</script>\n{# template #}\n',
            ["<!-- markup {{ something }} spanning a template -->", "/* theme */", "// script", "{# template #}"],
        ),
        (
            "embedded.py",
            "'''A documentation example with // literal.'''\n"
            'LABEL = "-- literal"\n'
            '_QUOTA_LUA = """\nlocal x = "-- value"\n-- Lua note\nreturn x\n"""\n'
            'conn.execute("""SELECT \'-- data\';\n-- SQL note\n""")\n'
            '_run_browser("""const x = "// literal";\n// Browser note\n""")\n'
            'subprocess.run([sys.executable, "-c", "# Embedded Python note\\nprint(1)"])\n',
            ["-- Lua note", "-- SQL note", "// Browser note", "# Embedded Python note"],
        ),
        (
            "values.yml",
            'label: "# quoted"  # suffix\nvalue: |\n  # data\nplain: token#fragment\n',
            ["# suffix"],
        ),
        (
            "values.sql",
            "SELECT $$-- dollar literal$$, '-- literal', E'edge\\'-- literal'; -- suffix\n"
            "/* outer /* nested */ tail */\n"
            "DO $code$ BEGIN\n  -- inside block\n  PERFORM 1;\nEND $code$;\n"
            "CREATE FUNCTION f() RETURNS int AS $$\n-- inside function\nSELECT 1;\n$$ LANGUAGE sql;\n",
            ["-- suffix", "/* outer /* nested */ tail */", "-- inside block", "-- inside function"],
        ),
        (
            "Dockerfile",
            "# syntax=docker/dockerfile:1\n# describe\nFROM scratch\n"
            'RUN echo "# literal" # command\nCMD ["thing", "# argument"]\n',
            ["# describe", "# command"],
        ),
        (
            "values.sh",
            "#!/bin/sh\nvalue=part#literal\ncat <<'EOF'\n# data\nEOF\necho \"${value#part}\" # prose\n",
            ["# prose"],
        ),
        (
            ".github/workflows/check.yml",
            "jobs:\n  lint:\n    steps:\n      - run: |\n          # command\n"
            '          echo "# literal"\n      - uses: actions/github-script@v7\n'
            "        with:\n          script: |\n            // action script\n"
            '            console.log("// data")\n      - run: "echo x # quoted shell"\n',
            ["# command", "// action script", "# quoted shell"],
        ),
    ],
)
def test_scans_comments_without_reporting_literals(monkeypatch, filename, source, expected) -> None:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from source_comments import prose_comments, source_format

    assert [comment.text for comment in prose_comments(source, source_format(filename))] == expected


def test_counts_the_actual_alloy_comment_line(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from check_comments import physical_lines
    from source_comments import prose_comments

    source = 'rule {\n  source = "// literal"\n\n  // description\n  rule = true\n}\n'
    comments = list(prose_comments(source, ".alloy"))
    assert len(comments) == 1
    assert comments[0].start == source.index("// description")
    assert physical_lines(source, comments) == (1, 1)


def test_ratchet_rejects_replacements_duplicates_and_reintroduction(tmp_path) -> None:
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def check(*args):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "check_comments.py"), *args],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            encoding="utf-8",
        )

    git("init", "-q", "-b", "work")
    source = tmp_path / "module.py"
    original = "# existing explanation\nvalue = 1\n"
    source.write_text(original, encoding="utf-8")
    git("add", "module.py")
    git("commit", "-qm", "baseline")
    git("branch", "upstream")
    git("branch", "--set-upstream-to=upstream")
    base = git("rev-parse", "HEAD")

    source.write_text("\n" + original, encoding="utf-8")
    assert check().returncode == 0
    source.write_text("# replacement-private-text\nvalue = 1\n", encoding="utf-8")
    replaced = check("--base", base)
    assert replaced.returncode == 1
    assert "module.py:1: new prose comment" in replaced.stdout
    assert "replacement-private-text" not in replaced.stdout + replaced.stderr

    source.write_text(original + "# existing explanation\n", encoding="utf-8")
    assert check().returncode == 1
    source.write_text("value = 1\n", encoding="utf-8")
    assert check().returncode == 0
    git("commit", "-qam", "remove the explanation")
    source.write_text(original, encoding="utf-8")
    assert check().returncode == 1

    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".env.py").write_bytes(b"\xff")
    (tmp_path / "README.md").write_text("<!-- documentation -->\n", encoding="utf-8")
    assert check().returncode == 0
    (tmp_path / "new.js").write_text("// refuse new sources too\n", encoding="utf-8")
    assert "new.js:1: new prose comment" in check().stdout

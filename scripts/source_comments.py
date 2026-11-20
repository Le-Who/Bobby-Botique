import ast
import io
import re
import tokenize
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import PurePosixPath

import yaml
from pygments.lexers import get_lexer_by_name
from pygments.token import Comment
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

LEXERS = {
    ".alloy": "hcl",
    ".cjs": "javascript",
    ".css": "css",
    ".html": "html+jinja",
    ".js": "javascript",
    ".jinja": "html+jinja",
    ".jinja2": "html+jinja",
    ".jsonc": "json",
    ".lua": "lua",
    ".mjs": "javascript",
    ".ps1": "powershell",
    ".sh": "bash",
    ".toml": "toml",
    ".ts": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}
LINE_FORMATS = {
    ".editorconfig": r"^[ \t]*([#;].*)",
    ".gitignore": r"^(#.*)",
    ".ini": r"^[ \t]*([#;].*)",
    ".service": r"^[ \t]*([#;].*)",
    ".timer": r"^[ \t]*([#;].*)",
    "redis.conf": r"^[ \t]*(#.*)",
}
EXCLUDED_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", "node_modules", "vendor", "dist", "build", "htmlcov"}
PYTHON_DIRECTIVES = (
    r"(?:ruff:\s*)?noqa(?:\s*:\s*[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)?"
    r"|type:\s*ignore(?:\[[a-z0-9_-]+(?:\s*,\s*[a-z0-9_-]+)*\])?"
    r"|fmt:\s*(?:off|on|skip)"
    r"|isort:\s*(?:off|on|skip|skip_file|split)"
    r"|pragma:\s*no (?:cover|branch)"
)
JS_DIRECTIVES = (
    r"eslint-(?:disable|enable)(?:-next-line|-line)?(?:\s+[@\w/-]+(?:\s*,\s*[@\w/-]+)*)?"
    r"|@ts-(?:check|nocheck|ignore|expect-error)"
    r"|[#@]\s*source(?:Mapping)?URL\s*=\s*\S+"
)
DIRECTIVES = {
    "python": PYTHON_DIRECTIVES,
    "javascript": JS_DIRECTIVES,
    "typescript": JS_DIRECTIVES,
    "bash": r"shellcheck\s+(?:disable=SC\d+(?:,SC\d+)*|shell=(?:bash|sh|dash|ksh))",
    "dockerfile": r"(?:syntax|escape|check)\s*=\s*[^\s]+",
}
LICENSE_DIRECTIVE = re.compile(r"SPDX-(?:License-Identifier|FileCopyrightText):\s*\S.*")
SQL_TOKEN = re.compile(
    r"--[^\n]*|/\*|(?<![\w$])[eE]'(?:\\.|''|[^'\\])*'|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
    r"|(?<![\w$])\$(?:[^\W\d]\w*)?\$|[;]|[^\W\d][\w$]*",
    re.DOTALL,
)
SQL_BLOCK = re.compile(r"/\*|\*/")
SQL_LANGUAGE = re.compile(r"\bLANGUAGE\s+(?:'|\")?(?:plpgsql|sql)\b", re.IGNORECASE)
DOCKER_SHELL = re.compile(r"^[ \t]*(?:RUN|CMD|ENTRYPOINT|HEALTHCHECK)\b([\s\S]*)", re.IGNORECASE)
JS_HELPERS = {
    "_run_browser": 0,
    "_run_browser_probe": 0,
    "_run_browser_regression": 1,
    "_run_dashboard_script": 0,
    "_run_state_browser": 1,
}
SQL_METHODS = {"execute", "executemany", "fetch", "fetchrow", "fetchval", "prepare"}


@dataclass(frozen=True)
class SourceComment:
    start: int
    end: int
    text: str
    lexer: str = ""


def source_format(path: str) -> str | None:
    posix = PurePosixPath(path)
    name = posix.name.lower()
    if (
        EXCLUDED_DIRECTORIES.intersection(posix.parts[:-1])
        or any(part.startswith((".pytest_", ".ruff_", ".mypy_")) for part in posix.parts[:-1])
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith((".min.js", ".min.css"))
    ):
        return None
    if name == "dockerfile" or name.startswith("dockerfile."):
        return "dockerfile"
    if name == ".dockerignore":
        return ".gitignore"
    if name in LINE_FORMATS:
        return name
    if path == "ops/observability/docker-api-proxy.cfg":
        return "haproxy"
    if posix.parts[:2] == (".github", "workflows") and posix.suffix.lower() in {".yml", ".yaml"}:
        return "workflow"
    suffix = posix.suffix.lower()
    if suffix in LEXERS or suffix in LINE_FORMATS or suffix in {".py", ".pyi", ".sql"}:
        return suffix
    return None


def _ranges(pattern: str, text: str) -> Iterator[SourceComment]:
    for match in re.finditer(pattern, text, re.MULTILINE):
        yield SourceComment(match.start(1), match.end(1), match[1])


@lru_cache
def _lexer(name: str):
    return get_lexer_by_name(name)


def _lex_comments(text: str, lexer: str) -> Iterator[SourceComment]:
    start = 0
    closing = ""
    source = text if text.endswith("\n") else text + "\n"
    for offset, token, value in _lexer(lexer).get_tokens_unprocessed(source):
        if token not in Comment or token in Comment.Preproc or not value or offset >= len(text):
            continue
        end = min(offset + len(value), len(text))
        if not closing:
            stripped = value.lstrip()
            start = offset + len(value) - len(stripped)
            if start >= end:
                continue
            value = stripped
            if value.startswith("/*"):
                closing = "*/"
            elif value.startswith("<!--"):
                closing = "-->"
        if not closing or source.endswith(closing, start, end):
            yield SourceComment(start, end, text[start:end], lexer)
            closing = ""
    if closing:
        yield SourceComment(start, len(text), text[start:], lexer)


def _python_comments(text: str) -> Iterator[SourceComment]:
    starts = [0, *(match.end() for match in re.finditer("\n", text))]
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            line, column = token.start
            start = starts[line - 1] + column
            yield SourceComment(start, start + len(token.string), token.string, "python")


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return PurePosixPath(node.value).name
    return ""


def _embedded_sources(node: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith(("_LUA", "_SQL")):
                yield node.value, target.id.rsplit("_", 1)[-1].lower()
                return
    if not isinstance(node, ast.Call) or not node.args:
        return
    function = _qualified_name(node.func)
    if function in JS_HELPERS and len(node.args) > JS_HELPERS[function]:
        yield node.args[JS_HELPERS[function]], "javascript"
    elif function.endswith(".register_script"):
        yield node.args[0], "lua"
    elif function.rpartition(".")[2] in SQL_METHODS and function.rpartition(".")[0].rpartition(".")[2] in {
        "conn",
        "connection",
        "pool",
        "db_conn",
        "db_pool",
    }:
        yield node.args[0], "sql"
    elif function in {"subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call"}:
        command = node.args[0]
        if isinstance(command, (ast.List, ast.Tuple)) and len(command.elts) >= 3:
            program, flag, body = command.elts[:3]
            if isinstance(flag, ast.Constant):
                if flag.value == "-e" and _qualified_name(program).lower() in {"node", "node.exe"}:
                    yield body, "javascript"
                elif flag.value == "-c" and _qualified_name(program).lower() in {
                    "python",
                    "python3",
                    "python.exe",
                    "sys.executable",
                }:
                    yield body, "python"


def _literal_parts(node: ast.AST) -> Iterator[ast.Constant]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _literal_parts(node.left)
        yield from _literal_parts(node.right)


def _python_embedded_comments(text: str) -> Iterator[SourceComment]:
    lines = text.split("\n")
    starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def position(line: int, column: int) -> int:
        return starts[line - 1] + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(text)
    seen = set()
    for node in ast.walk(tree):
        for source, lexer in _embedded_sources(node):
            for literal in _literal_parts(source):
                if (literal, lexer) in seen:
                    continue
                seen.add((literal, lexer))
                start = position(literal.lineno, literal.col_offset)
                end = position(literal.end_lineno, literal.end_col_offset)
                yield from _mapped_comments(literal.value, text[start:end], range(start, end), lexer, start)


def _sql_comments(text: str) -> Iterator[SourceComment]:
    position = 0
    words: list[str] = []
    statement_start = 0
    while match := SQL_TOKEN.search(text, position):
        token = match[0]
        position = match.end()
        if token.startswith("--"):
            yield SourceComment(match.start(), position, token)
        elif token == "/*":
            depth = 1
            for delimiter in SQL_BLOCK.finditer(text, position):
                depth += 1 if delimiter[0] == "/*" else -1
                if depth == 0:
                    position = delimiter.end()
                    break
            else:
                position = len(text)
            yield SourceComment(match.start(), position, text[match.start() : position])
        elif token.startswith("$"):
            close = text.find(token, position)
            if close < 0:
                break
            function = words[:1] == ["CREATE"] and any(word in words for word in ("FUNCTION", "PROCEDURE"))
            following = text[close + len(token) :].lstrip()
            language = SQL_LANGUAGE.search(text[statement_start : match.start()]) or SQL_LANGUAGE.match(following)
            if words[:1] == ["DO"] or (function and words[-1:] == ["AS"] and language):
                yield from _offset_comments(_sql_comments(text[position:close]), position)
            position = close + len(token)
        elif token == ";":
            words.clear()
            statement_start = position
        elif token[0].isalpha() and "'" not in token:
            words.append(token.upper())


def _offset_comments(comments: Iterator[SourceComment], offset: int) -> Iterator[SourceComment]:
    for comment in comments:
        yield SourceComment(comment.start + offset, comment.end + offset, comment.text, comment.lexer)


def _docker_comments(text: str) -> Iterator[SourceComment]:
    line_offset = 0
    command_start = 0
    command = ""
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            start = line_offset + len(line) - len(stripped)
            yield SourceComment(start, line_offset + len(line.rstrip("\n")), stripped.rstrip("\n"), "dockerfile")
        else:
            if not command:
                command_start = line_offset
            command += line
            if not line.rstrip().endswith("\\"):
                if match := DOCKER_SHELL.match(command):
                    body = match[1]
                    if body.lstrip()[:1] != "[":
                        yield from _offset_comments(_lex_comments(body, "bash"), command_start + match.start(1))
                command = ""
        line_offset += len(line)


def _mapping(node) -> dict[str, object]:
    if isinstance(node, MappingNode):
        return {key.value: value for key, value in node.value if isinstance(key, ScalarNode)}
    return {}


def _scalar(node, default: str = "") -> str:
    return node.value if isinstance(node, ScalarNode) else default


def _mapped_comments(
    value: str, raw: str, positions: range | list[int], lexer: str, start: int
) -> Iterator[SourceComment]:
    if lexer == "python":
        comments = list(_python_comments(value))
    elif lexer == "sql":
        comments = list(_sql_comments(value))
    else:
        comments = list(_lex_comments(value, lexer))
    if not comments:
        return
    mapping = SequenceMatcher(None, value, raw).get_matching_blocks()

    def original_offset(offset: int) -> int:
        for decoded, source, size in mapping:
            if size and offset < decoded + size:
                return positions[source + max(0, offset - decoded)]
        return positions[-1] if positions else start

    for comment in comments:
        source_start = original_offset(comment.start)
        source_end = original_offset(comment.end - 1) + 1
        yield SourceComment(source_start, source_end, comment.text, comment.lexer)


def _scalar_comments(text: str, node: ScalarNode, lexer: str) -> Iterator[SourceComment]:
    start = node.start_mark.index
    raw = text[start : node.end_mark.index]
    positions: range | list[int] = range(start, start + len(raw))
    if node.style in {"|", ">"}:
        header, _, raw = raw.partition("\n")
        start += len(header) + 1
        pieces = []
        positions = []
        lines = raw.splitlines(keepends=True)
        indent = min((len(line) - len(line.lstrip(" ")) for line in lines if line.strip()), default=0)
        offset = start
        for line in lines:
            trim = min(indent, len(line) - len(line.lstrip(" ")))
            pieces.append(line[trim:])
            positions.extend(range(offset + trim, offset + len(line)))
            offset += len(line)
        raw = "".join(pieces)
    yield from _mapped_comments(node.value, raw, positions, lexer, start)


def _workflow_comments(text: str) -> Iterator[SourceComment]:
    yield from _lex_comments(text, "yaml")
    workflow = _mapping(yaml.compose(text, Loader=yaml.BaseLoader))
    workflow_shell = _mapping(_mapping(workflow.get("defaults")).get("run")).get("shell")
    for node in _mapping(workflow.get("jobs")).values():
        job = _mapping(node)
        job_shell = _mapping(_mapping(job.get("defaults")).get("run")).get("shell", workflow_shell)
        steps = job.get("steps")
        if not isinstance(steps, SequenceNode):
            continue
        for step in steps.value:
            fields = _mapping(step)
            script = fields.get("run")
            if isinstance(script, ScalarNode):
                shell = _scalar(fields.get("shell", job_shell), "bash").partition(" ")[0]
                if shell in {"bash", "sh", "pwsh", "powershell", "python", "cmd"}:
                    lexer = {"sh": "bash", "pwsh": "powershell", "cmd": "bat"}.get(shell, shell)
                    yield from _scalar_comments(text, script, lexer)
            action = _scalar(fields.get("uses")).partition("@")[0].lower()
            script = _mapping(fields.get("with")).get("script")
            if isinstance(script, ScalarNode) and action in {"actions/github-script", "appleboy/ssh-action"}:
                yield from _scalar_comments(text, script, "javascript" if action == "actions/github-script" else "bash")


def _comments(text: str, format_name: str) -> Iterator[SourceComment]:
    if format_name in {".py", ".pyi"}:
        yield from _python_comments(text)
        if format_name == ".py":
            yield from _python_embedded_comments(text)
    elif format_name == ".sql":
        yield from _sql_comments(text)
    elif format_name == "dockerfile":
        yield from _docker_comments(text)
    elif format_name == "workflow":
        yield from _workflow_comments(text)
    elif format_name == "haproxy":
        for match in re.finditer(r"\\[^\n]|\"(?:\\.|[^\"\\])*\"|'[^']*'|#[^\n]*", text):
            if match[0].startswith("#"):
                yield SourceComment(match.start(), match.end(), match[0])
    elif format_name in LINE_FORMATS:
        yield from _ranges(LINE_FORMATS[format_name], text)
    else:
        yield from _lex_comments(text, LEXERS[format_name])


def prose_comments(text: str, format_name: str) -> Iterator[SourceComment]:
    for span in sorted(_comments(text, format_name), key=lambda comment: comment.start):
        comment = span.text.rstrip()
        if span.start == 0 and span.lexer in {"python", "bash", "javascript"} and re.match(r"#![ \t]*/\S+", comment):
            continue
        if (
            format_name in {".py", ".pyi"}
            and text.count("\n", 0, span.start) < 2
            and re.fullmatch(r"#\s*(?:-\*-\s*)?coding[:=]\s*utf-?8(?:\s*-\*-)?", comment, re.IGNORECASE)
        ):
            continue
        body = re.sub(r"^(?:<!--|/\*+|//|--|\{#|[;#])\s*|\s*(?:-->|\*/|#\})$", "", comment)
        directive = DIRECTIVES.get(span.lexer)
        if not LICENSE_DIRECTIVE.fullmatch(body) and not (directive and re.fullmatch(directive, body)):
            yield SourceComment(span.start, span.end, comment, span.lexer)

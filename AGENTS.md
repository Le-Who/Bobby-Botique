# AGENTS.md — Rules for AI Coding Agents

This file documents critical rules for any AI coding agent (Copilot, Antigravity, Jules,
Codex, Claude, etc.) working in this repository.

## 🔴 CRITICAL: File Encoding Rules

**All files in this repository are UTF-8.** This is especially critical for:
- `README.md`
- `CHANGELOG.md`
- Any `.py`, `.html`, `.js`, `.ts`, `.json` file

### Mandatory encoding rules

1. **Always open files with `encoding='utf-8'`**:
   ```python
   # ✅ CORRECT
   content = open("README.md", encoding="utf-8").read()
   open("README.md", "w", encoding="utf-8").write(content)

   # ❌ WRONG — uses system default (cp1251 on this Windows machine)
   content = open("README.md").read()
   open("README.md", "w").write(content)
   ```

2. **Never "convert" or "fix" encoding** unless you have verified the file is actually
   corrupt. Garbled display in a Windows cp1251 terminal does NOT mean the file is broken.
   Use `python -c "open('README.md', encoding='utf-8').read(); print('OK')"` to verify.

3. **Never replace emoji with `?`, `???`, or escape sequences** like `\u2699`.
   Emoji must be stored as raw Unicode codepoints in UTF-8 files.

4. **Do not use smart-quotes normalization** on Markdown files. The curly quotes
   `"`, `"`, `'`, `'` must NOT replace UTF-8 continuation bytes.

5. **Verify before AND after** any write to a documentation file:
   ```python
   import subprocess
   subprocess.run(['python', 'scripts/check_encoding.py'])
   ```

### Why this matters

On this Windows development machine, the default Python encoding is **cp1251**.
When an agent calls `open("README.md")` without specifying encoding, Python reads
the bytes with cp1251. Multi-byte UTF-8 sequences (like emoji `E2 9A 99` for ⚙️)
will either raise `UnicodeDecodeError` or be silently mangled with `errors='replace'`.

The resulting data, when written back to disk as UTF-8, produces mojibake patterns
like `â\x9a\x99` (3 separate codepoints) instead of `⚙️` (1 emoji).

**This happened in commit `4556976` and corrupted 60+ emoji across 25 subsequent
commits before being detected and fixed in `c3ea7b0`.**

## Pre-commit Hook

A pre-commit hook at `.git/hooks/pre-commit` runs `scripts/check_encoding.py`
automatically before each commit. It will **block the commit** if broken patterns
are detected in README.md or CHANGELOG.md.

If you are making automated commits (e.g. CI), ensure the pre-commit hook passes
before merging.

## Terminal Output

The Windows terminal (cp1251) will display UTF-8 emoji as garbled characters when
printing to stdout. This is a **display-only issue** — the file on disk is correct.
Do not "fix" files based on how they look in terminal output.

To safely print UTF-8 content to a Windows terminal, use:
```python
import os; os.environ['PYTHONUTF8'] = '1'
# or
sys.stdout.reconfigure(encoding='utf-8')
```

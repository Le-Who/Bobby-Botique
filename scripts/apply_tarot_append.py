"""
Helper script to extract the NEW_BLOCK from patch_inline_tarot.py and append it to app/handlers/inline.py.
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
INLINE_PY = ROOT / "app" / "handlers" / "inline.py"
PATCH_PY = ROOT / "scripts" / "patch_inline_tarot.py"

# Read patch script to extract NEW_BLOCK
with open(PATCH_PY, encoding="utf-8") as f:
    content = f.read()

# We know the content has NEW_BLOCK = """..."""
# Let's extract the block of text inside the triple quotes of NEW_BLOCK
start_marker = 'NEW_BLOCK = """\\'
end_marker = '"""'

start_idx = content.find(start_marker)
if start_idx == -1:
    raise ValueError("Could not find start marker in patch script.")

start_idx += len(start_marker)
end_idx = content.find(end_marker, start_idx)
if end_idx == -1:
    raise ValueError("Could not find end marker in patch script.")

new_block_content = content[start_idx:end_idx]

# Clean up escape sequences (e.g. \\n -> \n, \\" -> ")
new_block_content = new_block_content.replace("\\n", "\n").replace('\\"', '"')

# Append to app/handlers/inline.py
with open(INLINE_PY, encoding="utf-8") as f:
    inline_lines = f.readlines()

# Verify that the last line is clean (ends with a newline)
if inline_lines and not inline_lines[-1].endswith("\n"):
    inline_lines[-1] += "\n"

# Check if _build_fortune_cookie_html definition is already in the file (just in case)
inline_content = "".join(inline_lines)
if "def _build_fortune_cookie_html" in inline_content:
    print("Warning: _build_fortune_cookie_html already exists in inline.py. No changes made.")
else:
    # Append the new block
    with open(INLINE_PY, "w", encoding="utf-8") as f:
        f.write(inline_content + "\n" + new_block_content)
    print("Successfully appended tarot block to inline.py!")

"""JSON extraction utilities."""

import json


def extract_json_object(text: str) -> dict | None:
    """Extract the first valid JSON object from noisy model output.

    Supports:
    - Code fence wrapping (```json ... ```)
    - Extra text before/after the object
    - Multiple JSON structures: takes the first valid object with required fields
    - `system_prompt` → `prompt` field normalization
    """
    if not text:
        return None
    cleaned = text.strip()

    # Strip outer code fence
    if cleaned.startswith("```"):
        lines = cleaned.split("\\n")
        if len(lines) > 1:
            cleaned = "\\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # Strip textual prefixes like `json`/`JSON`
    lower = cleaned.lstrip()
    for prefix in ("json\\n", "json\\r\\n", "json ", "JSON\\n", "JSON\\r\\n", "JSON "):
        if lower.startswith(prefix):
            cleaned = cleaned[len(cleaned) - len(lower) + len(prefix) :].lstrip()
            break

    # Walk all '{' occurrences and try to assemble balanced object
    n = len(cleaned)
    for i in range(n):
        if cleaned[i] != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for j in range(i, n):
            ch = cleaned[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = cleaned[i : j + 1]
                        try:
                            obj = json.loads(candidate)
                        except Exception:
                            break  # Current block invalid, try next i
                        if isinstance(obj, dict):
                            # Normalize system_prompt → prompt
                            if "prompt" not in obj and "system_prompt" in obj:
                                obj["prompt"] = obj.get("system_prompt")
                            # Require mandatory fields
                            if all(k in obj for k in ("title", "purpose", "prompt")):
                                return obj
                        break
    return None

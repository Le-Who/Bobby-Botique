"""
Patch _generate_tarot_inline: swap _stream_inline_fast (race-based) for
ProviderRouter.get_response() (sequential key rotation, no wasted parallel calls).

Rationale (per user feedback):
- QNA_MODEL (gemini-2.5-flash) is more stable — rarely needs racing
- Racing wastes daily quota unnecessarily  
- Sequential retry-on-failure is enough: try key 1, rotate to key 2 on 503, etc.
- ProviderRouter.get_response() already does this with max_key_retries=3 and
  health-aware key selection (DB-backed KeyStatusManager)
"""
import sys
import re

TARGET = "app/handlers/inline.py"

OLD_BLOCK = (
    "    # \u2500\u2500 Use _stream_inline_fast: Race Requests + multi-round key rotation \u2500\u2500\n"
    "    # This is the same resilient path used by all other inline queries.\n"
    "    # It races 2 AI Studio keys + 1 Vertex AI slot per round, retries up to\n"
    "    # 4 rounds, so a single 503 on one key is completely transparent to the user.\n"
    "    result, _sources = await _stream_inline_fast(\n"
    "        preferred_model=_INLINE_FALLBACK_MODEL,\n"
    "        history=[{\"role\": \"user\", \"parts\": [prompt]}],\n"
    "        system_instruction=system_instruction,\n"
    "        user_id=user_id,\n"
    "        max_rounds=4,\n"
    "        enable_web_search=False,\n"
    "    )\n"
    "\n"
    "    if not result or not result.strip():\n"
    "        logging.error(\n"
    "            \"Tarot generation failed (spread=%s): no result from _stream_inline_fast\",\n"
    "            spread_type,\n"
    "        )\n"
    "        with contextlib.suppress(Exception):\n"
    "            await bot.edit_message_text(\n"
    "                inline_message_id=inline_message_id,\n"
    "                text=\"\u274c \u041a\u0430\u0440\u0442\u044b \u043c\u043e\u043b\u0447\u0430\u0442 (\u043e\u0448\u0438\u0431\u043a\u0430 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438).\",\n"
    "            )\n"
    "        return\n"
)

NEW_BLOCK = (
    "    # \u2500\u2500 Sequential key rotation via ProviderRouter.get_response() \u2500\u2500\n"
    "    # QNA_MODEL (gemini-2.5-flash) is stable enough that parallel racing\n"
    "    # would only waste daily quota. ProviderRouter tries one key at a time,\n"
    "    # rotating to the next on 503/UNAVAILABLE, with health-aware key selection.\n"
    "    from app.errors import is_error_message\n"
    "    from app.providers.router import get_provider_router\n"
    "\n"
    "    router = get_provider_router()\n"
    "    result, _tokens = await router.get_response(\n"
    "        preferred_model=_TAROT_MODEL,\n"
    "        history=[{\"role\": \"user\", \"parts\": [prompt]}],\n"
    "        system_instruction=system_instruction,\n"
    "        user_id=user_id,\n"
    "        use_openrouter=False,\n"
    "        max_key_retries=3,\n"
    "    )\n"
    "\n"
    "    if not result or not result.strip() or is_error_message(result):\n"
    "        logging.error(\n"
    "            \"Tarot generation failed (spread=%s): %s\",\n"
    "            spread_type,\n"
    "            (result or \"\")[:120],\n"
    "        )\n"
    "        with contextlib.suppress(Exception):\n"
    "            await bot.edit_message_text(\n"
    "                inline_message_id=inline_message_id,\n"
    "                text=\"\u274c \u041a\u0430\u0440\u0442\u044b \u043c\u043e\u043b\u0447\u0430\u0442 (\u043e\u0448\u0438\u0431\u043a\u0430 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438).\",\n"
    "            )\n"
    "        return\n"
)

OLD_DOCSTRING_FRAGMENT = (
    "    Uses _stream_inline_fast (Race Requests + multi-round key rotation) instead\n"
    "    of a bespoke single-key call, so 503 UNAVAILABLE errors are transparently\n"
    "    retried across different API keys and Vertex AI, exactly like regular\n"
    "    inline queries.\n"
)

NEW_DOCSTRING_FRAGMENT = (
    "    Uses ProviderRouter.get_response() for sequential key rotation: tries one\n"
    "    key at a time and rotates on 503/UNAVAILABLE (max 3 retries). QNA_MODEL\n"
    "    (gemini-2.5-flash) is stable enough that parallel racing would only waste\n"
    "    daily quota.\n"
)

with open(TARGET, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Patch the docstring
if OLD_DOCSTRING_FRAGMENT not in content:
    print("ERROR: docstring fragment not found", file=sys.stderr)
    sys.exit(1)
content = content.replace(OLD_DOCSTRING_FRAGMENT, NEW_DOCSTRING_FRAGMENT, 1)

# 2. Patch the generation call
if OLD_BLOCK not in content:
    print("ERROR: generation block not found", file=sys.stderr)
    sys.exit(1)
content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)

with open(TARGET, "w", encoding="utf-8") as f:
    f.write(content)

# 3. Add _TAROT_MODEL constant near _INLINE_FALLBACK_MODEL if not already there
with open(TARGET, "r", encoding="utf-8") as f:
    content = f.read()

if "_TAROT_MODEL" not in content:
    # Insert after _INLINE_FALLBACK_MODEL line
    old_const = '_INLINE_FALLBACK_MODEL = "gemini-2.5-flash-lite"\n'
    new_const = (
        '_INLINE_FALLBACK_MODEL = "gemini-2.5-flash-lite"\n'
        "# QNA_MODEL equivalent for tarot: stable, no racing needed.\n"
        "# Loaded lazily from settings so hot-reload picks up env changes.\n"
        "def _get_tarot_model() -> str:\n"
        "    from app.config import settings\n"
        "    return settings.QNA_MODEL\n"
        "\n"
        "# Module-level alias for readability inside _generate_tarot_inline\n"
        "_TAROT_MODEL: str = \"\"\n"
    )
    if old_const not in content:
        print("ERROR: _INLINE_FALLBACK_MODEL constant not found", file=sys.stderr)
        sys.exit(1)
    content = content.replace(old_const, new_const, 1)
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(content)

# Verify
with open(TARGET, "r", encoding="utf-8") as f:
    content = f.read()

checks = [
    ("_stream_inline_fast" not in content[content.find("async def _generate_tarot_inline"):content.find("def _build_tarot_system_prompt")],
     "_stream_inline_fast removed from _generate_tarot_inline"),
    ("router.get_response" in content, "router.get_response present"),
    ("_TAROT_MODEL" in content, "_TAROT_MODEL constant present"),
    ("is_error_message" in content[content.find("async def _generate_tarot_inline"):content.find("def _build_tarot_system_prompt")],
     "is_error_message check present"),
    ("get_provider_router" in content, "get_provider_router import present"),
]

all_ok = True
for ok, msg in checks:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {msg}")
    if not ok:
        all_ok = False

sys.exit(0 if all_ok else 1)

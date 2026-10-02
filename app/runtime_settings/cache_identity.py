"""Cache namespaces for outputs whose instructions or routes can change live."""

import hashlib
import json
from collections.abc import Mapping

from app.runtime_settings.lifecycle import operation_snapshot


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


async def cache_identity(process: str, identity: str, *prompts: str) -> str:
    """Preserve legacy keys until relevant overrides exist; ignore unrelated edits."""
    snapshot = await operation_snapshot()
    keys = {f"process:{process}", *(f"prompt:{name}" for name in prompts)}
    values = {key: _plain(snapshot.values[key]) for key in keys if key in snapshot.values}
    if not values:
        return identity
    payload = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{identity}:cfg:{digest}"

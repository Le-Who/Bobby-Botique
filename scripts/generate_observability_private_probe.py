"""Write one synthetic privacy event through the actual application log writer."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def generate_private_probe(output: Path) -> None:
    # Logging resolution reads this synthetic mapping, without application .env.
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        LOG_FORMAT="json",
        LOG_CONTENT_MODE="full",
        SERVICE_NAME="gemaibotv2",
        APP_ENV="synthetic",
    )
    from app.observability.content import content_fields
    from app.observability.context import request_scope
    from app.observability.events import emit
    from app.observability.redaction import provider_key_fields
    from app.utils.logging_config import setup_detailed_logging, shutdown_detailed_logging

    credential = "synthetic-protected-provider-" + uuid4().hex
    birth = "synthetic-forbidden-birth-body-" + uuid4().hex
    memory = "synthetic-forbidden-memory-body-" + uuid4().hex
    request_id = uuid4().hex
    captured = io.StringIO()
    with redirect_stdout(captured):
        setup_detailed_logging(enable_structured_logging=True)
        try:
            with request_scope(request_id=request_id, user_id=123):
                emit(
                    "synthetic.collector_privacy",
                    message="Controlled provider echo " + credential,
                    credential_echo={"api_key": credential},
                    birth=content_fields("natal_chart_input", birth, sensitive=True),
                    private_memory=content_fields("memory_source", memory, sensitive=True),
                    **provider_key_fields("synthetic", credential),
                )
        finally:
            shutdown_detailed_logging()
    events = [json.loads(line) for line in captured.getvalue().splitlines()]
    selected = [event for event in events if event["event"] == "synthetic.collector_privacy"]
    assert len(selected) == 1, "the actual writer must produce exactly one privacy event"
    event = selected[0]
    wire = json.dumps(event, ensure_ascii=False)
    assert all(secret not in wire for secret in (credential, birth, memory)), "synthetic protected input leaked"
    expected = {
        "request_id": request_id,
        "key_suffix": credential[-4:],
        "key_fingerprint": hashlib.sha256(credential.encode()).hexdigest()[:16],
        "birth_chars": len(birth),
        "memory_chars": len(memory),
        "birth_fingerprint": hashlib.sha256(birth.encode()).hexdigest()[:16],
        "memory_fingerprint": hashlib.sha256(memory.encode()).hexdigest()[:16],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"event": event, "expected": expected}, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate_private_probe(args.output)
    print("Synthetic application privacy event written; protected input excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Observe actual server close events without relying on Quart's test-client exceptions."""

import asyncio
from typing import Any
from urllib.parse import urlsplit


async def websocket_close_event(application: Any, url: str, *, timeout: float = 1) -> dict[str, Any]:
    """Run one ASGI connection, preserving its close code and all task ownership."""
    parts = urlsplit(url)
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    closed = asyncio.get_running_loop().create_future()
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": parts.path,
        "raw_path": parts.path.encode("utf-8"),
        "query_string": parts.query.encode("ascii"),
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 1),
        "server": ("localhost", 80),
        "subprotocols": [],
        "extensions": {},
    }

    async def send(event):
        if event["type"] == "websocket.close" and not closed.done():
            closed.set_result(dict(event))
            incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    task = asyncio.create_task(application(scope, incoming.get, send))
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait({task, closed}, return_when=asyncio.FIRST_COMPLETED)
            # Awaiting the app preserves failures and finishes disconnect cleanup.
            await task
            if not closed.done():
                raise AssertionError("Application finished without a websocket.close event")
            return closed.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        closed.cancel()

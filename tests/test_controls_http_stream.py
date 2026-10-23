"""Exercise controls body limits through the production Hypercorn HTTP/1.1 path."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config


async def test_controls_body_discards_empty_chunks_without_losing_the_payload():
    from app.web_controls import _ControlsBody

    body = _ControlsBody(None)
    for _ in range(10_000):
        await body.put(b"")
    payload = b'{"expected_revision": 0}'
    await body.put(payload)
    body.set_complete()
    assert await body.get() == payload
    assert await body.get() == b""


@pytest.fixture
async def controls_server(monkeypatch):
    from app.web import quart_app

    config = Config()
    config.bind = ["127.0.0.1:0"]
    config.accesslog = None
    config.errorlog = None
    config.graceful_timeout = 1
    sockets = config.create_sockets()
    port = sockets.insecure_sockets[0].getsockname()[1]
    # Retain the reserved ephemeral socket; do not race a second bind.
    monkeypatch.setattr(config, "create_sockets", lambda: sockets)
    previous_secret = quart_app.secret_key
    stop = asyncio.Event()
    server = asyncio.create_task(serve(quart_app, config, shutdown_trigger=stop.wait))
    try:
        async with asyncio.timeout(3):
            while True:
                if server.done():
                    await server
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.01)
        signer = quart_app.session_interface.get_signing_serializer(quart_app)
        cookie = signer.dumps({"authenticated": True, "controls_csrf": "stream-test-csrf"})
        yield port, cookie
    finally:
        stop.set()
        try:
            await asyncio.wait_for(server, timeout=3)
        finally:
            if not server.done():
                server.cancel()
            await asyncio.gather(server, return_exceptions=True)
            for sock in sockets.insecure_sockets:
                sock.close()
            quart_app.secret_key = previous_secret


@asynccontextmanager
async def chunked_request(server, *, csrf="stream-test-csrf"):
    port, cookie = server
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            (
                "POST /api/admin/controls/preview HTTP/1.1\r\n"
                "Host: localhost\r\n"
                f"Cookie: gembot_session={cookie}\r\n"
                f"X-CSRF-Token: {csrf}\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


async def send_chunk(writer, data):
    writer.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
    await writer.drain()


async def response_status(reader):
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)
    return int(headers.split(b" ", 2)[1])


@pytest.mark.parametrize("chunk_sizes", [(300_001,), (150_000, 150_001)], ids=["single", "cumulative"])
async def test_hypercorn_rejects_oversized_controls_before_chunked_eof(controls_server, chunk_sizes):
    with patch("app.web_controls.set_value", new_callable=AsyncMock) as write:
        async with chunked_request(controls_server) as (reader, writer):
            for size in chunk_sizes:
                await send_chunk(writer, b" " * size)
            # No terminating chunk is sent: receiving 413 must not depend on EOF.
            assert await response_status(reader) == 413
        write.assert_not_awaited()


async def test_hypercorn_parses_exact_limit_json_after_chunked_eof(controls_server):
    payload = json.dumps({"section": "limit", "id": "gemini-test", "value": 100, "expected_revision": 0}).encode()
    body = payload + b" " * (300_000 - len(payload))
    async with chunked_request(controls_server) as (reader, writer):
        await send_chunk(writer, body[:150_000])
        await send_chunk(writer, body[150_000:])
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        assert await response_status(reader) == 200
        response = await asyncio.wait_for(reader.read(), timeout=1)
        assert json.loads(response)["valid"] is True


async def test_hypercorn_still_rejects_invalid_csrf_before_chunked_eof(controls_server):
    with patch("app.web_controls.set_value", new_callable=AsyncMock) as write:
        async with chunked_request(controls_server, csrf="invalid") as (reader, writer):
            await send_chunk(writer, b"{}")
            assert await response_status(reader) == 403
        write.assert_not_awaited()

"""Close assertions must observe the server event and drain their ASGI task."""

import asyncio

import pytest
from quart import Quart, websocket


async def test_close_observer_preserves_the_actual_server_code_and_reason():
    from tests.asgi_websocket import websocket_close_event

    app = Quart(__name__)

    @app.websocket("/reject")
    async def reject():
        await websocket.close(4403, "Access revoked")

    event = await websocket_close_event(app, "/reject")
    assert event == {"type": "websocket.close", "code": 4403, "reason": "Access revoked"}


async def test_close_observer_cancels_and_awaits_a_timed_out_application():
    from tests.asgi_websocket import websocket_close_event

    cleaned = asyncio.Event()

    async def application(scope, receive, send):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(TimeoutError):
        await websocket_close_event(application, "/never-closes", timeout=0.01)
    assert cleaned.is_set()


async def test_close_observer_preserves_application_errors_and_awaits_cleanup():
    from tests.asgi_websocket import websocket_close_event

    cleaned = asyncio.Event()

    async def application(scope, receive, send):
        try:
            raise RuntimeError("synthetic ASGI failure")
        finally:
            cleaned.set()

    with pytest.raises(RuntimeError, match="synthetic ASGI failure"):
        await websocket_close_event(application, "/error")
    assert cleaned.is_set()


async def test_close_observer_awaits_cleanup_when_its_caller_is_cancelled():
    from tests.asgi_websocket import websocket_close_event

    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def application(scope, receive, send):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    observer = asyncio.create_task(websocket_close_event(application, "/cancelled"))
    await asyncio.wait_for(started.wait(), timeout=1)
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert cleaned.is_set()

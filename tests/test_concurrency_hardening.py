import ast
from pathlib import Path


def _module(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text())


def test_role_custom_retry_registered_once():
    text = Path('app/handlers/callbacks.py').read_text()
    assert text.count('pattern="^role_custom_retry$"') == 1


def test_heavy_callback_semaphore_present_and_used():
    tree = _module('app/handlers/callbacks.py')
    source = Path('app/handlers/callbacks.py').read_text()
    assert '_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore' in source

    async_with_count = source.count('async with _HEAVY_CALLBACK_SEMAPHORE')
    assert async_with_count >= 2


def test_heavy_message_semaphore_present_and_used():
    source = Path('app/handlers/messages.py').read_text()
    assert '_HEAVY_REQUEST_SEMAPHORE = asyncio.Semaphore' in source
    # regular long request path + media-group heavy path
    assert source.count('async with _HEAVY_REQUEST_SEMAPHORE') >= 2

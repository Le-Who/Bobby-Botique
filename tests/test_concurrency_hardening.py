import ast
from pathlib import Path


def _module(path: str) -> ast.Module:
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def test_role_custom_retry_registered_once():
    text = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert text.count('pattern="^role_custom_retry$"') == 1


def test_heavy_callback_semaphore_present_and_used():
    _module("app/handlers/callbacks.py")
    source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "_HEAVY_CALLBACK_SEMAPHORE = asyncio.Semaphore" in source

    async_with_count = source.count("async with _HEAVY_CALLBACK_SEMAPHORE")
    assert async_with_count >= 2


def test_heavy_message_semaphore_present_and_used():
    source = Path("app/handlers/messages.py").read_text(encoding="utf-8")
    assert "_HEAVY_REQUEST_SEMAPHORE = asyncio.Semaphore" in source
    # regular long request path (messages.py) + media-group heavy path (msg_media.py)
    media_source = Path("app/handlers/msg_media.py").read_text(encoding="utf-8")
    combined_count = (
        source.count("async with _HEAVY_REQUEST_SEMAPHORE")
        + media_source.count("async with _HEAVY_REQUEST_SEMAPHORE")
    )
    assert combined_count >= 2


# ── Streaming lock guard tests ───────────────────────────────────────────────


def test_is_user_busy_helper_exists():
    """Verify the _is_user_busy helper is defined in callbacks.py."""
    source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")
    assert "def _is_user_busy(user_id" in source
    assert "_BUSY_TOAST" in source


def test_streaming_lock_guards_on_state_mutating_callbacks():
    """All 6 state-mutating callbacks must check _is_user_busy before mutation.

    These handlers perform Read-Modify-Write on chat_state. Without the guard,
    they race with the streaming handler's final update_user_chat, causing
    the user's intent (model switch, history clear, search toggle) to be silently lost.
    """
    source = Path("app/handlers/callbacks.py").read_text(encoding="utf-8")

    # Each handler function should contain "_is_user_busy" somewhere in its body
    handlers_needing_guard = [
        "model_button_callback",
        "switch_model_callback",
        "new_topic_callback",
        "new_chat_callback",
        "deep_dive_callback",
        "toggle_search_callback",
    ]

    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in handlers_needing_guard:
                func_source = ast.get_source_segment(source, node)
                assert func_source is not None, f"Could not get source for {node.name}"
                assert "_is_user_busy" in func_source, (
                    f"{node.name} is missing _is_user_busy guard — "
                    f"it mutates chat_state and will race with streaming"
                )
                handlers_needing_guard = [
                    h for h in handlers_needing_guard if h != node.name
                ]

    assert not handlers_needing_guard, (
        f"Handlers not found in callbacks.py: {handlers_needing_guard}"
    )


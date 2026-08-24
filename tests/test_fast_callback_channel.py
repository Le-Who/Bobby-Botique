import ast
from pathlib import Path


def _parse_module(path: str):
    return ast.parse(Path(path).read_text(encoding="utf-8"))


def test_fast_callback_channel_uses_non_blocking_handlers():
    tree = _parse_module("app/handlers/callbacks.py")

    register_fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register")

    fast_calls = [
        node
        for node in register_fn.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_add_fast_callback"
    ]

    assert fast_calls, "register() should configure a dedicated fast callback channel"


def test_fast_callback_helper_sets_block_false_and_priority_group():
    tree = _parse_module("app/handlers/callbacks.py")
    helper_fn = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_add_fast_callback"
    )

    add_handler_call = next(
        node.value
        for node in helper_fn.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "add_handler"
    )

    callback_handler_call = add_handler_call.args[0]
    assert isinstance(callback_handler_call, ast.Call)
    assert isinstance(callback_handler_call.func, ast.Name)
    assert callback_handler_call.func.id == "CallbackQueryHandler"

    has_block_false = any(
        kw.arg == "block" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in callback_handler_call.keywords
    )
    assert has_block_false, "fast callback helper must use block=False"

    has_group_minus_one = any(
        kw.arg == "group"
        and isinstance(kw.value, ast.UnaryOp)
        and isinstance(kw.value.op, ast.USub)
        and isinstance(kw.value.operand, ast.Constant)
        and kw.value.operand.value == 1
        for kw in add_handler_call.keywords
    )
    assert has_group_minus_one, "fast callback helper must use high-priority group=-1"


def test_application_uses_user_scoped_concurrency_in_both_builder_paths():
    source = Path("bot.py").read_text(encoding="utf-8")
    assert source.count(".concurrent_updates(UserScopedUpdateProcessor(50))") == 2
    assert ".concurrent_updates(50)" not in source

import ast
import glob
import os


def find_await_in_loops(filepath):
    with open(filepath, encoding='utf-8') as f:
        try:
            tree = ast.parse(f.read(), filename=filepath)
        except Exception:
            return

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            # check if any child is an Await node
            for child in ast.walk(node):
                if isinstance(child, ast.Await):
                    # Check if it awaits a database call, API call, or similar
                    if isinstance(child.value, ast.Call):
                        func = child.value.func
                        func_name = ""
                        if isinstance(func, ast.Name):
                            func_name = func.id
                        elif isinstance(func, ast.Attribute):
                            func_name = getattr(func.value, 'id', '') + '.' + func.attr
                        
                        print(f"File {filepath}:{child.lineno} - loop contains await {func_name}()")

for file in glob.glob("app/**/*.py", recursive=True):
    find_await_in_loops(file)

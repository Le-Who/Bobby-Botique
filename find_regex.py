import ast
import os
import glob

def find_re_compile_in_funcs(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=filepath)
    except Exception:
        return

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    func_name = ""
                    if isinstance(func, ast.Name):
                        func_name = func.id
                    elif isinstance(func, ast.Attribute):
                        func_name = getattr(func.value, 'id', '') + '.' + func.attr
                    
                    # looking for re.compile or re.sub/match inside functions
                    if func_name in ("re.compile", "re.sub", "re.match", "re.findall", "re.finditer"):
                        print(f"File {filepath}:{child.lineno} - {node.name} contains {func_name}()")

for file in glob.glob("app/**/*.py", recursive=True):
    find_re_compile_in_funcs(file)

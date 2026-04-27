import ast
import glob


def find_nested(file):
    try:
        with open(file, encoding='utf-8') as f:
            t = ast.parse(f.read())
    except Exception: return
    for node in ast.walk(t):
        if isinstance(node, (ast.For, ast.While, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            for child in ast.walk(node):
                if child is not node and isinstance(child, (ast.For, ast.While)):
                    line = getattr(node, 'lineno', 0)
                    print(f'{file}:{line}')
                    break

for code_file in glob.glob('app/**/*.py', recursive=True):
    find_nested(code_file)

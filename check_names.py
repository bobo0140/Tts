"""
check_names.py — предпазител срещу липсващи имена.

Грешката "name 'SPAM_WINDOW_SECONDS' is not defined" се появи чак когато
кодът стигна до реален потребител, защото Python открива такива неща
едва при изпълнение на конкретния ред.

Този скрипт ги хваща статично и се пуска при всяко построяване.
"""

import ast
import builtins
import sys
from pathlib import Path

FILES = ["engine.py", "web_main.py", "core_lib.py", "api_module.py", "live_module.py"]


def collect_defined(tree) -> set:
    names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    for el in t.elts:
                        if isinstance(el, ast.Name):
                            names.add(el.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.comprehension)):
            tgt = node.target
            if isinstance(tgt, ast.Name):
                names.add(tgt.id)
            elif isinstance(tgt, (ast.Tuple, ast.List)):
                for el in tgt.elts:
                    if isinstance(el, ast.Name):
                        names.add(el.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in node.args.args + node.args.kwonlyargs:
                names.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def check(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined = collect_defined(tree)

    # аргументите на всички функции
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in a.args + a.kwonlyargs + a.posonlyargs:
                defined.add(arg.arg)
            if a.vararg:
                defined.add(a.vararg.arg)
            if a.kwarg:
                defined.add(a.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            for arg in node.args.args:
                defined.add(arg.arg)

    # Проверяваме КОНСТАНТИТЕ (главни букви) — там бяха пропуските и
    # там една липса чупи цял модул по време на работа.
    problems = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id.isupper() and len(node.id) > 2
                and node.id not in defined):
            problems.append((node.lineno, node.id))
    return problems


def main() -> int:
    base = Path(__file__).parent
    bad = False
    for name in FILES:
        p = base / name
        if not p.exists():
            continue
        problems = check(p)
        if problems:
            bad = True
            print(f"✗ {name}:")
            for line, ident in sorted(set(problems)):
                print(f"    ред {line}: '{ident}' не е дефинирано или внесено")
        else:
            print(f"✓ {name}")
    if bad:
        print("\nПострояването се спира — поправи имената по-горе.")
        return 1
    print("\nВсички имена са налични.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""轻量静态检查：找出未使用的 import 与明显未定义的模块级名字。

不引入 ruff/flake8 依赖 —— 离线环境里能跑就够了。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def unused_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                imported[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                imported[name] = node.lineno

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
    # 字符串注解 / __all__ 里出现的名字也算使用
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for token in node.value.replace("|", " ").replace("[", " ").replace("]", " ").split():
                used.add(token.strip("',\""))
    return [(line, name) for name, line in sorted(imported.items(), key=lambda kv: kv[1]) if name not in used]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["sglbench", "tests", "tools"])
    args = parser.parse_args()
    problems = 0
    for root in args.paths:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for line, name in unused_imports(path):
                print(f"{path}:{line}: 未使用的 import: {name}")
                problems += 1
    if problems:
        print(f"\n共 {problems} 处")
    else:
        print("未发现未使用的 import")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

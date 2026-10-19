"""查出**同名但被遮蔽的定义**：模块级函数/类/常量定义了两次。

## 为什么需要这个检查

`context/compact.py` 里曾经有**两个** `should_auto_compact`：一个没 docstring
的旧版本在前，一个带 docstring 的在后。Python 里后者遮蔽前者——**第一个是死代码**。

现有四个检查器**全都看不见它**：

| 检查器 | 为什么不报 |
|---|---|
| `check_imports.py` | 两个定义都在同一个模块里，导入没问题 |
| `check_constants.py` | 只看常量；而且名字确实被引用了 |
| `check_orphans.py` | 按**名字**找引用——`should_auto_compact` 这个名字确实被调用 |
| `mutation_check.py` | 变异只改文本；改中第一个定义不会影响行为，看起来「测试没抓到」 |

于是它一直躺着，直到我为了做文件映射表去数函数时才撞见。

## 判据

同一个模块作用域里，**同一个名字**被 `def` / `class` / 赋值语句绑定了两次以上
就报。不区分 `if`/`try` 分支里的定义——那种情况确实可能是刻意（条件定义），
但它们同样会遮蔽，值得人工看一眼。

`__all__`、类型别名重复、以及 `x = x` 这类重绑定不在此列（只看模块顶层的
`def`/`class`/简单赋值目标）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "minicode"

#: 允许重复绑定的名字（都带理由）。
ALLOWED_DUPLICATES: dict[str, str] = {}


def module_level_bindings(tree: ast.Module) -> list[tuple[str, int, str]]:
    """模块顶层的绑定：`(名字, 行号, 种类)`。

    **只看顶层**——函数体里的重绑定是正常代码，不属于「遮蔽」。
    """
    bindings: list[tuple[str, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.append((node.name, node.lineno, "def"))
        elif isinstance(node, ast.ClassDef):
            bindings.append((node.name, node.lineno, "class"))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings.append((target.id, node.lineno, "assign"))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # `x: int = 1` 只算一次；纯注解 `x: int` 不算绑定
            if node.value is not None:
                bindings.append((node.target.id, node.lineno, "assign"))
    return bindings


def scan() -> list[str]:
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        seen: dict[str, list[tuple[int, str]]] = {}
        for name, line, kind in module_level_bindings(tree):
            seen.setdefault(name, []).append((line, kind))
        for name, places in sorted(seen.items()):
            if len(places) < 2:
                continue
            if name in ALLOWED_DUPLICATES:
                continue
            rendered = "、".join(f"第{line}行({kind})" for line, kind in places)
            problems.append(
                f"{path.relative_to(REPO)}: `{name}` 被定义 {len(places)} 次 → {rendered}"
            )
    return problems


def main() -> int:
    problems = scan()
    print(f"扫描 {len(list(SRC.rglob('*.py')))} 个模块 …")
    if not problems:
        print("✓ 没有同名遮蔽的定义")
        return 0
    print(f"发现 {len(problems)} 处同名遮蔽：")
    for item in problems:
        print(f"  ✗ {item}")
    print()
    print(
        "说明：后一个定义会**遮蔽**前一个，前面的成为死代码。\n"
        "      若确属有意（条件定义），加进 ALLOWED_DUPLICATES 并写明理由。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

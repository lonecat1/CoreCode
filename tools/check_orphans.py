"""零依赖的「孤儿函数」核查器。

## 它抓什么

`src/corecode` 里**定义了但没有任何地方引用**的模块级函数与类。

这就是 `check_constants.py` 抓不到的那一类缺陷。它的存在有具体理由：

`find_matching_slash_commands()` 被定义、被 docstring 说明、看起来完全正常，
但**没有任何调用点**，也没有测试。它就这样躺了很久，直到我为了核对
`completeSlashCommand` 才偶然发现——而用户感受到的是「Tab 补全没反应」。

`check_constants.py` 只扫**模块级赋值**（常量），函数根本不在它的视野里。
常量孤儿通常是「写多了」，函数孤儿往往是「**该接线但没接**」——后者更严重，
因为它描述的是一个缺失的功能，而代码读起来像功能已经有了。

## 判定口径

对每个模块级 `def` / `class`：

1. 在整个 `src/` + `tests/` 里统计它名字的出现次数；
2. **定义行本身不算引用**（在定义文件里减 1）；
3. 结果为 0 → 孤儿。

这样「只在同文件内部被调用」也算有引用——那是正常用法。

## 白名单

少数函数**故意**零引用，各有理由，逐条写在 `ALLOWED_ORPHANS` 里。

白名单是双向的：既有的条目如果已经不再零引用（说明它被接线了），也会被
报出来提醒删掉白名单条目——否则白名单会长期漂移、失去意义。
"""

from __future__ import annotations

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _shared import force_utf8_output  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "corecode")
TESTS = os.path.join(REPO, "tests")

#: 故意零引用的函数/类，键是 `相对路径::名字`。每条都必须写清理由。
#:
#: 目前为空——写这个工具时白名单里放了两条入口函数（`cli.py::main`、
#: `__main__.py::main`），结果**工具自己把它们报了出来**：`main` 其实被
#: `__main__.py` 和 `if __name__ == "__main__"` 引用着，本来就不该进白名单。
#: 这正是双向校验的价值。
ALLOWED_ORPHANS: dict[str, str] = {}

#: 不计入核查的名字。以下划线开头的私有辅助**仍然检查**——它们才是孤儿高发区。
SKIP_NAMES: set[str] = {
    "__init__",
    "__main__",
    "__repr__",
    "__eq__",
    "__hash__",
    "__post_init__",
    "__enter__",
    "__exit__",
    "__iter__",
    "__len__",
    "__str__",
    "__bool__",
    "__contains__",
    "__call__",
}


def module_sources() -> dict[str, str]:
    """`相对路径 -> 源码`，src 与 tests 全部。"""
    out: dict[str, str] = {}
    for root in (SRC, TESTS):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__" and d != "_tmp"]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, REPO).replace("\\", "/")
                try:
                    with open(full, encoding="utf-8", errors="replace") as handle:
                        out[rel] = handle.read()
                except OSError:
                    continue
    return out


def definitions(sources: dict[str, str]) -> list[tuple[str, str, int]]:
    """收集 `src` 里的模块级函数与类：`(相对路径, 名字, 行号)`。"""
    found: list[tuple[str, str, int]] = []
    for rel, text in sorted(sources.items()):
        if not rel.startswith("src/"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in SKIP_NAMES:
                    continue
                found.append((rel, node.name, node.lineno))
    return found


def count_references(
    name: str, def_rel: str, sources: dict[str, str]
) -> int:
    """统计名字在 src+tests 里的出现次数，**定义行本身不算**。"""
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    total = 0
    for rel, text in sources.items():
        hits = len(pattern.findall(text))
        if rel == def_rel:
            # 定义行恰好出现一次，不算引用。
            hits -= 1
        total += hits
    return max(0, total)


def main() -> int:
    force_utf8_output()
    sources = module_sources()
    items = definitions(sources)
    print(f"扫描 {len(items)} 个模块级函数/类 …")

    orphans: list[tuple[str, str, int]] = []
    for rel, name, line in items:
        if count_references(name, rel, sources) == 0:
            orphans.append((rel, name, line))

    ok = True
    reported: set[str] = set()

    print()
    if orphans:
        unexpected = []
        for rel, name, line in orphans:
            key = f"{os.path.basename(rel)}::{name}"
            if key in ALLOWED_ORPHANS:
                reported.add(key)
                continue
            unexpected.append((rel, name, line))
        if unexpected:
            ok = False
            print(f"✗ {len(unexpected)} 个零引用函数/类（既没被调用，也不在白名单里）：")
            for rel, name, line in unexpected:
                print(f"    {rel}:{line}  {name}")
            print()
            print("  处理方式二选一：")
            print("    · 接线：它本该被调用（这正是 `find_matching_slash_commands` 的情况）")
            print("    · 删除：它根本不需要——留下「看起来能用」的死代码比缺失更糟")
            print("  确属故意零引用（如入口函数）时，加进 ALLOWED_ORPHANS 并写清理由。")
        else:
            print(f"✓ 零引用的 {len(orphans)} 个都在白名单里")
    else:
        print("✓ 没有零引用的函数或类")

    # 白名单双向校验：已经不再零引用的条目要删掉。
    stale = [
        key
        for key in ALLOWED_ORPHANS
        if key not in reported
    ]
    if stale:
        ok = False
        print()
        print(f"✗ 白名单里有 {len(stale)} 条已经不再零引用（或名字写错了），请删掉：")
        for key in stale:
            print(f"    {key}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

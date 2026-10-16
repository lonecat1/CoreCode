"""零依赖的常量核查：找出「定义了但没人用」的常量。

## 为什么需要它

原 TypeScript 版有 **4 个零引用常量**（`SUMMARY_MAX_OUTPUT_TOKENS`、
`PTL_MAX_RETRIES`、`MIN_KEEP_TOKENS`、`BLOCKED_UTILIZATION`），本实现一度把
其中几个连着一起继承了，甚至把 `SUMMARY_MAX_OUTPUT_TOKENS` 继承成了
**两份互不引用的定义**（`constants.py` 与 `compact.py` 各一份）。

这些都不是靠「读一遍代码」能可靠发现的——常量通常在文件头部排成一列，
每一行看起来都同样合理。**只有把它们当成数据、逐个去搜引用，才看得出来。**

零引用常量的代价不是那几行代码，而是**后来者会以为它生效**：
改它的数字不会改变任何行为，而改动者会以为改了。

## 它检查什么

对每个候选常量名，在 `src/` 与 `tests/` 下搜索**除定义处以外**的出现：

  1. 哪里都没有 → **孤儿**（报告）
  2. 只被 `tests/` 引用 → **测试专用**（报告，需要判断是否合理：
     有的是「给测试用的钩子」，有的只是测试顺手引用了本该删掉的东西）
  3. 只出现在注释/文档字符串里 → **伪引用**（报告，这类更隐蔽：
     看着「有引用」，实际没有任何代码读它）
  4. 至少有一处 `src/` 里的真实代码引用 → 通过

第 2、3 类是本脚本的主要价值。人肉核对时容易被注释骗过——我自己就被骗过一次：
把 `MIN_KEEP_TOKENS` 的注释行当成了「有人在用」。

## 用法

    python tools/check_constants.py

退出码 0 表示无问题；1 表示发现孤儿 / 测试专用 / 伪引用。
"""

from __future__ import annotations

import ast
import os
import sys

from _shared import force_utf8_output

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "minicode")
TESTS = os.path.join(REPO, "tests")

#: 刻意允许「仅测试引用」的常量。
#:
#: 每一条都要写明理由——否则这个白名单会变成掩盖问题的垃圾桶。
#: 判据是：**它存在的目的就是被测试读取**，生产代码不需要它。
ALLOWED_ORPHANS: dict[str, str] = {
    "CLAUDE_SETTINGS_PATH": (
        "测试用它断言 `claude_settings_path()` **没有**跟随 MINICODE_HOME。"
        "这是那条隔离性质的反面参照物，生产代码只调用函数、不需要这个快照。"
    ),
    "PENDING_SUBAGENT_TOOLS": (
        "子 Agent 的「进行中」工具名集合。生产代码按状态字符串判断，"
        "测试用它遍历所有待处理工具名来断言状态机。"
    ),
    "SIDE_EFFECTING_COMMANDS": (
        "给测试用的钩子：`test_probing_commands_does_not_touch_the_repository` "
        "用它确认「会改仓库的命令」被登记了。生产代码不需要读它——"
        "它描述的是**测试该怎么探测**，不是运行时行为。"
    ),
}


class Occurrence:
    __slots__ = ("path", "line", "text", "is_comment")

    def __init__(self, path: str, line: int, text: str, is_comment: bool) -> None:
        self.path = path
        self.line = line
        self.text = text
        self.is_comment = is_comment

    def describe(self) -> str:
        rel = os.path.relpath(self.path, REPO).replace("\\", "/")
        kind = "注释" if self.is_comment else "代码"
        return f"{rel}:{self.line} [{kind}] {self.text.strip()[:70]}"


def module_sources() -> dict[str, str]:
    """`src/` 与 `tests/` 下的全部 Python 源文件。

    **测试也要扫。** 只扫 `src/` 会把「只有测试在用」的常量误报成孤儿
    ——`SIDE_EFFECTING_COMMANDS` 就是这样：它是给测试用的钩子，
    生产代码不需要引用它。
    """
    sources: dict[str, str] = {}
    for base in (SRC, TESTS):
        for root, _dirs, files in os.walk(base):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8") as handle:
                        sources[path] = handle.read()
    return sources


def defined_constants(sources: dict[str, str]) -> dict[str, str]:
    """找出 `src/` 里模块级的大写常量名 -> 定义它的文件。

    **只收 `src/` 的定义。** 测试文件里也有大量大写常量（`_HERE`、`_TMP`、
    `WORKDIR` …），它们是测试自己的局部设定，不是「本项目的常量」——
    把它们报出来只会淹没真正的问题。
    """
    found: dict[str, str] = {}
    for path, text in sources.items():
        if not os.path.abspath(path).startswith(os.path.abspath(SRC)):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            # 只认模块级赋值：NAME = ... 或 NAME: T = ...
            targets: list[str] = []
            if isinstance(node, ast.Assign):
                targets = [
                    t.id for t in node.targets if isinstance(t, ast.Name)
                ]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target.id]
            for name in targets:
                if name.isupper() and len(name) > 2:
                    found.setdefault(name, path)
    return found


def find_occurrences(
    name: str, sources: dict[str, str], definition_path: str
) -> list[Occurrence]:
    """找出名字的所有出现，并区分「代码引用」与「注释提及」。

    ## 为什么必须区分

    注释里的出现会让人以为常量在用。我自己核对 `MIN_KEEP_TOKENS` 时就被
    模块 docstring 里的一行骗过，一度以为它「与 MAX_KEEP_TOKENS 成对使用」。
    """
    hits: list[Occurrence] = []
    for path, text in sources.items():
        lines = text.splitlines()
        # 用 AST 找出所有真实的字符串字面量区间（docstring），
        # 落在这些区间里的行算「注释/文档」而不是代码。
        doc_ranges: list[tuple[int, int]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    body = getattr(node, "body", None)
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        doc_ranges.append(
                            (body[0].lineno, body[0].end_lineno or body[0].lineno)
                        )

        for index, raw in enumerate(lines, 1):
            if name not in raw:
                continue
            # 定义处本身不算引用。
            if path == definition_path and _is_definition_line(raw, name):
                continue
            stripped = raw.lstrip()
            in_doc = any(lo <= index <= hi for lo, hi in doc_ranges)
            is_comment = stripped.startswith("#") or in_doc
            hits.append(Occurrence(path, index, raw, is_comment))
    return hits


def _is_definition_line(line: str, name: str) -> bool:
    stripped = line.strip()
    for prefix in (f"{name} =", f"{name}:", f"{name}="):
        if stripped.startswith(prefix):
            return True
    return False


def main() -> int:
    sources = module_sources()
    constants = defined_constants(sources)

    orphans: list[tuple[str, str]] = []
    tests_only: list[tuple[str, list[Occurrence]]] = []
    fake: list[tuple[str, list[Occurrence]]] = []
    stale_whitelist: list[tuple[str, str]] = []

    print(f"检查 {len(constants)} 个模块级常量（扫描 src/ 与 tests/）…")

    for name, path in sorted(constants.items()):
        hits = find_occurrences(name, sources, path)
        real = [hit for hit in hits if not hit.is_comment]
        src_real = [
            hit for hit in real if os.path.abspath(hit.path).startswith(os.path.abspath(SRC))
        ]
        if name in ALLOWED_ORPHANS:
            if src_real:
                # 白名单项一旦有了生产引用，就该从白名单里拿掉——
                # 否则白名单会慢慢积累已失效的条目，最终没人再敢信它。
                stale_whitelist.append((name, src_real[0].describe()))
            continue
        if not hits:
            orphans.append((name, path))
        elif not real:
            fake.append((name, hits))
        elif not src_real:
            tests_only.append((name, real))

    ok = True

    if stale_whitelist:
        ok = False
        print()
        print(f"白名单已过期（{len(stale_whitelist)} 个）——它们现在有生产引用了：")
        for name, where in stale_whitelist:
            print(f"  - {name}  ({where})")
        print()
        print("  请从 ALLOWED_ORPHANS 移除。留着的后果是白名单慢慢变成噪音，")
        print("  而下一次真正需要加白名单时，没人会认真读它。")

    if orphans:
        ok = False
        print()
        print(f"零引用常量（{len(orphans)} 个）——哪里都没有出现：")
        for name, path in orphans:
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            print(f"  - {name}  ({rel})")
        print()
        print("  代价不是那几行代码，而是**后来者会以为它生效**：")
        print("  改它的数字不会改变任何行为，而改动者会以为改了。")
        print("  要么接线，要么删除，要么加进 ALLOWED_ORPHANS 并写明理由。")

    if tests_only:
        ok = False
        print()
        print(f"仅测试引用（{len(tests_only)} 个）——生产代码不读它：")
        for name, hits in tests_only:
            print(f"  - {name}")
            for hit in hits[:3]:
                print(f"      {hit.describe()}")
        print()
        print("  这些有的是「给测试用的钩子」（合理），有的只是测试顺手引用了")
        print("  本该删掉的东西。逐个判断，加进 ALLOWED_ORPHANS 时写明属于哪种。")

    if fake:
        ok = False
        print()
        print(f"伪引用常量（{len(fake)} 个）——只被注释/文档提到，没有任何代码读它：")
        for name, hits in fake:
            print(f"  - {name}")
            for hit in hits:
                print(f"      {hit.describe()}")
        print()
        print("  这类比零引用更隐蔽：扫一眼会觉得「有引用」，而实际没有。")

    if ALLOWED_ORPHANS:
        print()
        print(f"白名单（{len(ALLOWED_ORPHANS)} 个，均为「仅测试引用」）：")
        for name, reason in sorted(ALLOWED_ORPHANS.items()):
            print(f"  - {name}: {reason}")

    if ok:
        print()
        print(f"✓ 全部 {len(constants)} 个常量都有生产代码引用，或已列入白名单")
        return 0
    return 1


if __name__ == "__main__":
    force_utf8_output()
    raise SystemExit(main())

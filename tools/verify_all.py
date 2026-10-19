"""一条命令跑完全部验证。

## 为什么要这个

现在有 7 个独立检查器，各自回答一个不同的问题：

| 脚本 | 回答什么问题 |
|---|---|
| `check_imports.py` | 导入都能解析吗？有没有从不被导入的模块？ |
| `check_constants.py` | 每个常量都有人用吗？（含**伪引用**：只在注释里出现） |
| `check_orphans.py` | 每个函数/类都有人调吗？ |
| `check_duplicates.py` | 有没有同名定义被遮蔽（前面那个是死代码）？ |
| `file_map.py` | 每个 TS 文件都有对应实现吗？有没有凭空多出的模块？ |
| `e2e_compare.py` | 真实交互下两边行为一样吗？ |
| `compaction_compare.py` | 四级压缩的判定一样吗？ |
| `mcp_compare.py` | MCP 线协议下两边的视图一样吗？ |
| `mutation_check.py` | 测试真的会变红吗？ |

手动跑一遍要记 9 条命令，还容易漏。这个脚本把它们串起来，**失败就停**，
并给出统一的摘要。

## 三档

`--fast` 只跑**不依赖 TS 仓库**的那些（常量/孤儿/遮蔽/映射/单元测试），
十几秒出结果，适合每次改完代码。

默认再加**三组跨实现对照**——它们需要 `D:\\CoreCode\\MiniCode` 在场，
慢一些但这才是「是否真的一样」的证据。

`--full` 再加**变异检查**（约 1.5 小时）。它默认不跑，因为它会**改写源码**，
不能与任何编辑或其它检查并行。

## 一个必须写下来的坑

`--full` 期间**不能编辑 `src/`，也不能并行跑别的测试**——变异检查会反复把
源码改成各种错误版本，任何同时进行的操作都会看到残缺状态。

我在这上面踩过两次，症状都表现为「某个不相关模块的测试失败」，很难往变异
检查上联想。所以这个脚本在跑变异检查前会**显式提示**，并且在结束时检查有没有
留下 `.mutation-backup` 残留。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

#: `(相对脚本路径, 一句话说明, 需要 TS 仓库, 是否属于 --full)`
CHECKS: list[tuple[str, str, bool, bool]] = [
    ("check_imports.py", "导入可解析 / 无从不被导入的模块", False, False),
    ("check_constants.py", "常量都有引用（含伪引用检查）", False, False),
    ("check_orphans.py", "函数与类都有引用", False, False),
    ("check_duplicates.py", "无同名遮蔽的定义", False, False),
    ("file_map.py", "逐文件映射覆盖完整", False, False),
    ("e2e_compare.py", "端到端交互与文件结果一致", True, False),
    ("compaction_compare.py", "四级压缩判定一致", True, False),
    ("mcp_compare.py", "MCP 线协议视图一致", True, False),
    ("mutation_check.py", "变异全部被测试杀死", False, True),
]

#: 单元测试单独一项（它不是 `tools/` 里的脚本）。
UNIT_TESTS = ("unittest", "单元测试全通过")


def run_one(label: str, command: list[str]) -> tuple[bool, float, str]:
    started = time.monotonic()
    proc = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    return ok, elapsed, output


def summarize(output: str) -> str:
    """从输出里挑一行最有信息量的，作为摘要。"""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(("✓", "OK", "FAILED", "Ran ", "变异检查", "发现 ")):
            return line
    return lines[-1] if lines else "（无输出）"


def main(argv: list[str]) -> int:
    fast = "--fast" in argv
    full = "--full" in argv
    if fast and full:
        print("--fast 与 --full 不能同时用")
        return 2

    selected = [
        (path, label, needs_ts, is_full)
        for path, label, needs_ts, is_full in CHECKS
        if (not is_full or full) and (not needs_ts or not fast)
    ]

    results: list[tuple[str, bool, float, str]] = []

    print("=" * 78)
    print(" 单元测试")
    print("=" * 78)
    ok, elapsed, output = run_one(
        "unittest", [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    )
    results.append((UNIT_TESTS[1], ok, elapsed, summarize(output)))
    for line in output.splitlines():
        if line.startswith(("Ran ", "OK", "FAILED")):
            print(f"  {line.strip()}")
    if not ok:
        print(output)
        print("\n单元测试不通过，后续检查没有意义。")
        return 1

    for path, label, needs_ts, is_full in selected:
        print()
        print("=" * 78)
        print(f" {label}  （{path}）")
        print("=" * 78)
        if is_full:
            print("  ⚠ 接下来会反复改写 src/ —— 不要编辑源码，也不要并行跑别的测试")
        ok, elapsed, output = run_one(label, [PY, str(REPO / "tools" / path)])
        results.append((label, ok, elapsed, summarize(output)))
        for line in output.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(("扫描", "=====", "------")):
                print(f"  {stripped[:150]}")
        if not ok and not is_full:
            print(f"\n{label} 不通过，停止。")
            return 1

    print()
    print("=" * 78)
    print(" 摘要")
    print("=" * 78)
    for label, ok, elapsed, summary in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label:<32} {elapsed:6.1f}s  {summary[:80]}")

    failed = [label for label, ok, _e, _s in results if not ok]
    if failed:
        print(f"\n失败：{failed}")
        return 1

    # --full 之后清点残留
    if full:
        leftovers = list(REPO.rglob("*.mutation-backup"))
        if leftovers:
            print(f"\n⚠ 变异检查留下了 {len(leftovers)} 个备份文件：")
            for item in leftovers:
                print(f"    {item}")
            return 1

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

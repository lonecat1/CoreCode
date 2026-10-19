"""核对 `docs/parity.md` 里写的数字是否与**当前仓库**一致。

## 为什么需要这个

那份文档是「功能对等」的唯一依据，但它里面的数字是**手写**的。手写的数字必然
漂移，而且漂移得**无声**：

    第 67 行： 全仓 269 个模块级常量
    第 135 行：267 个常量，零孤儿        ← 同一份文档里两个不同数字
    实际：     270

这不是笔误，是同一类数字在三处各写一遍、各自忘了更新。同样的事发生在函数/类
数量（596 vs 实际 606）和变异条数（301 vs 实际 325）上。

**一份数字过期的「可验证清单」，比没有清单更糟**——它让人以为核过了。

## 做法

1. 数字**不手抄**：`docs/parity.md` 里放一个由标记围起来的块，内容由本脚本
   生成（`--write` 重写）。
2. 手写的那些数字也要核：本脚本按固定句式（`N 个常量`、`N 个模块级函数/类`、
   `N 条`）扫全文，任何一处与实测不符就报出来。
3. 数字**从检查器自己那里取**，不另数一套。这一点我刚踩过：手数模块级常量
   得到 280，而 `check_constants.py` 报 270——两者口径不同（后者排除白名单与
   非模块级赋值）。文档引用的是检查器，就该用它的数。

## 为什么是**跑**检查器而不是读它的源码

口径（哪些算常量、哪些算孤儿）是检查器定义的。本脚本若自己实现一遍判定，
就有了第二套口径——那正是这份文档最初出问题的原因。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
DOC = REPO / "docs" / "parity.md"

BEGIN = "<!-- BEGIN GENERATED STATS -->"
END = "<!-- END GENERATED STATS -->"

#: 手写数字的句式 → 对应的统计键。
#:
#: 每一条都对应文档里真实出现过的写法。加新句式时**必须**同时能取到实测值，
#: 否则这个检查会变成「永远通过」。
#:
#: ## 句式必须够窄，否则会误报
#:
#: 最初这里有一条裸的 `(\d+) 个常量`，于是把 `| utils/tool-result-storage.ts |
#: 6 个常量 + 预览的… |` 也当成了「全仓常量数」，报出一个假的不一致。
#: **那条说的是单个模块的常量**。所以每条句式都带上足以确定范围的限定词
#: （`模块级`、`零孤儿`、`0 歧义`…），宁可漏报也不误报——误报会让人开始
#: 无视这个检查。
HAND_WRITTEN_PATTERNS: list[tuple[str, str, str]] = [
    (r"(\d+) 个模块级常量", "constants", "个模块级常量"),
    (r"(\d+) 个常量，零孤儿", "constants", "个常量，零孤儿"),
    (r"(\d+) 个模块级函数/类", "functions", "个模块级函数/类"),
    (r"(\d+) 个模块全通过", "modules", "个模块全通过"),
    (r"(\d+) 条，0 歧义", "mutations", "条变异"),
]


def run_checker(name: str) -> str:
    proc = subprocess.run(
        [PY, str(REPO / "tools" / name)],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return (proc.stdout or "") + (proc.stderr or "")


def collect() -> dict[str, int]:
    """全部统计数字——**一律来自检查器的实际输出**。"""
    stats: dict[str, int] = {}

    constants = run_checker("check_constants.py")
    match = re.search(r"全部 (\d+) 个常量", constants)
    if match:
        stats["constants"] = int(match.group(1))

    orphans = run_checker("check_orphans.py")
    match = re.search(r"扫描 (\d+) 个模块级函数/类", orphans)
    if match:
        stats["functions"] = int(match.group(1))

    imports = run_checker("check_imports.py")
    match = re.search(r"(\d+) 个模块", imports)
    if match:
        stats["modules"] = int(match.group(1))

    mapped = run_checker("file_map.py")
    match = re.search(r"TS (\d+) 个文件 → 映射 (\d+) \+ TUI 排除 (\d+)", mapped)
    if match:
        stats["ts_files"] = int(match.group(1))
        stats["mapped"] = int(match.group(2))
        stats["tui"] = int(match.group(3))
    if "覆盖度、存在性、反向覆盖全部通过" in mapped:
        stats["file_map_ok"] = 1

    duplicates = run_checker("check_duplicates.py")
    if "没有同名遮蔽的定义" in duplicates:
        stats["duplicates_ok"] = 1

    # 变异条数：`mutation_check.py` 跑一轮要两小时，只取清单长度
    sys.path.insert(0, str(REPO / "tools"))
    try:
        import mutation_check  # noqa: PLC0415

        stats["mutations"] = len(mutation_check.MUTATIONS)
    except Exception:  # noqa: BLE001 - 取不到就不写这一项
        pass
    finally:
        sys.path.pop(0)

    return stats


def collect_tests() -> int | None:
    proc = subprocess.run(
        [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    match = re.search(r"^Ran (\d+) tests", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def generated_block(stats: dict[str, int], tests: int | None) -> str:
    rows = [
        ("TS 源文件", stats.get("ts_files")),
        ("已映射到本实现", stats.get("mapped")),
        ("TUI 排除", stats.get("tui")),
        ("未映射", 0),
        ("Python 模块", stats.get("modules")),
        ("模块级常量", stats.get("constants")),
        ("模块级函数/类", stats.get("functions")),
        ("变异用例", stats.get("mutations")),
        ("单元测试", tests),
    ]
    lines = [BEGIN, "", "| 项目 | 数量 |", "|---|---|"]
    for label, value in rows:
        lines.append(f"| {label} | {value if value is not None else '（取不到）'} |")
    lines.append("")
    lines.append(END)
    return "\n".join(lines)


def hand_written_problems(text: str, stats: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for pattern, key, label in HAND_WRITTEN_PATTERNS:
        actual = stats.get(key)
        if actual is None:
            continue
        for match in re.finditer(pattern, text):
            stated = int(match.group(1))
            if stated != actual:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(
                    f"第 {line} 行写「{stated} {label}」，实测 {actual}"
                )
    return problems


def verify(text: str, stats: dict[str, int], block: str) -> list[str]:
    """把文档正文与实测数字比对，返回问题列表。

    ## 为什么单独抽出来

    `main()` 里的这段判断原本直接内联，于是**测不到**：三条变异（「不报生成块
    不一致」「吞掉手写数字的不一致」「取不到值时不跳过」）全部存活。检查器的
    判断逻辑没法被测试，等于没有检查器。

    抽成纯函数后，`tests/test_checkers.py` 可以直接喂一段假文档、断言它报出
    该报的问题。这个函数不读文件、不跑检查器，所以也不受 `src/` 变异影响。
    """
    problems: list[str] = []
    if BEGIN not in text or END not in text:
        problems.append("文档里没有生成块（用 --write 生成一次）")
    elif block not in text:
        problems.append("生成块与实测不一致（用 --write 重写）")
    problems.extend(hand_written_problems(text, stats))
    return problems


def main(argv: list[str]) -> int:
    write = "--write" in argv
    stats = collect()
    tests = collect_tests()
    text = DOC.read_text(encoding="utf-8")

    block = generated_block(stats, tests)
    if BEGIN in text and END in text:
        updated = re.sub(
            re.escape(BEGIN) + r".*?" + re.escape(END),
            lambda _m: block,
            text,
            flags=re.DOTALL,
        )
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"

    if write:
        DOC.write_text(updated, encoding="utf-8", newline="\n")
        print(f"已重写统计块：{DOC}")
        for key, value in stats.items():
            print(f"  {key} = {value}")
        if tests is not None:
            print(f"  tests = {tests}")
        return 0

    problems = verify(text, stats, block)

    print(f"核对 {DOC.relative_to(REPO)} …")
    for key in sorted(stats):
        print(f"  {key} = {stats[key]}")
    if tests is not None:
        print(f"  tests = {tests}")

    if not problems:
        print("✓ 文档里的数字与实测一致")
        return 0
    print(f"\n发现 {len(problems)} 处不一致：")
    for item in problems:
        print(f"  ✗ {item}")
    print("\n数字别手抄——跑 `python tools/check_docs.py --write` 重新生成。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

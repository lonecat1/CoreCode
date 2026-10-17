"""端到端行为对照：同一个场景分别跑 TS 原版与 Python 版，diff 输出。

## 为什么需要这个

前面所有逐模块的核对都是**局部证据**——每个常量、每张表、每个函数都对上了，
但「它们组合起来的行为」是否一致，只有跑一遍才知道。

## 怎么做到可比

`--mock` 模式不需要 API key，且两边都有一个「按输入前缀发工具调用」的假模型：

    /ls .      → list_files → 「目录内容如下：…」
    /read f    → read_file  → 「文件内容如下：…」
    /cmd pwd   → run_command→ 原样返回
    其他        → 直接给最终回答

于是 `--mock -p "<prompt>"` 能驱动一次**完整回合**：适配器 → agent loop →
工具执行 → 结果格式化 → 最终回答 → 终端渲染。

## 口径

只比对**语义可判定**的内容。路径、时间、耗时、会话 id 这些天然不同，会被
归一化掉（见 `normalize()`）。归一化必须是「可知的差异」，不能把真实差异也
抹掉——所以每条规则都写明它抹的是什么。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
TS_ROOT = Path(r"D:\CoreCode\MiniCode")
PY_ROOT = Path(r"D:\CoreCode\minicode-py")

#: `tsx` 的入口。用 `node_modules\.bin\tsx.cmd` 而不是 `npx`：
#: Windows 上 `npx` 只有 `.ps1` 包装，`subprocess` 直接调用找不到。
TSX = str(TS_ROOT / "node_modules" / ".bin" / "tsx.cmd")


def normalize(text: str, ts_home: str, py_home: str) -> str:
    """抹掉**必然不同**的部分。每条规则都注明抹的是什么。"""
    out = text.replace("\r\n", "\n")
    # 1) 解释器/工具链的绝对路径（两边的 python 不是同一个）
    out = out.replace(PY, "<PY>")
    out = re.sub(r"[A-Za-z]:\\[^\s\"']*?\\python\.exe", "<PY>", out)
    # 2) 临时目录的绝对路径（两边各自建了一个）
    out = out.replace(ts_home, "<HOME>").replace(py_home, "<HOME>")
    out = re.sub(r"[A-Za-z]:\\[^\s\"']*?[\\/](?:tmp|Temp)[\\/][^\s\"']*", "<TMP>", out)
    # 3) 会话 id / 时间戳 / 耗时
    out = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<UUID>", out)
    out = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", "<TS>", out)
    out = re.sub(r"\d+\.\d+s", "<DUR>", out)
    out = re.sub(r"\d+ 次工具调用", "<N> 次工具调用", out)
    # 4) ANSI 颜色（两边的着色实现不同，但都属于「装饰」）
    out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out)
    return out


def run_ts(args: list[str], cwd: str, home: str, stdin: str | None = None) -> str:
    # 用**原版拼写** `MINI_CODE_HOME`：两边都认它，隔离才有效。
    # （最初这里写的是 `MINICODE_HOME`，TS 不认，于是它读写真实 home——
    #  diff 里出现的 `C:\Users\...\.mini-code\mcp.json` 就是这么来的。）
    env = {**os.environ, "MINI_CODE_HOME": home, "NO_COLOR": "1", "FORCE_COLOR": "0"}
    proc = subprocess.run(
        [TSX, "src/index.ts", *args],
        cwd=str(TS_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=180, shell=True,
        input=stdin if stdin is not None else "",
    )
    return proc.stdout + proc.stderr


def run_py(args: list[str], cwd: str, home: str, stdin: str | None = None) -> str:
    env = {
        **os.environ,
        "MINI_CODE_HOME": home,
        "PYTHONPATH": str(PY_ROOT / "src"),
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    }
    proc = subprocess.run(
        [PY, "-m", "minicode", *args],
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=180,
        input=stdin if stdin is not None else "",
    )
    return proc.stdout + proc.stderr


#: `(标签, 命令行参数, 喂给 stdin 的内容, 期望)`。
#:
#: **必须用 stdin 驱动，不能用 `-p`**：原版**没有** `-p/--prompt` 标志，它会
#: 无视掉然后进 REPL。用 `-p` 对照等于在测两个不同的东西。stdin 是两边唯一的
#: 共同驱动方式，也顺带覆盖了 REPL 这条路径。
#:
#: `期望` 有三种：
#:
#:   - `"identical"`：归一化后必须逐字相同。**这一类失败就是 bug。**
#:   - `"banner_only"`：只比较**框体行**（`╭ │ ╰` 开头）。用于「框体必须逐字一致，
#:     但周边有行模式必需的额外输出」的场景。
#:   - `"intentional"`：已知的有意差异（本地化、排版）。只报告，不计入失败。
#:     每一条都必须写明差异是什么、为什么有意——否则它就会变成「懒得管」。
SCENARIOS: list[tuple[str, list[str], str | None, str]] = [
    ("--help", ["--help"], None, "identical"),
    ("mcp list（空）", ["mcp", "list"], None, "identical"),
    ("skills list（空）", ["skills", "list"], None, "identical"),
    ("mcp 未知子命令", ["mcp", "nonsense"], None, "identical"),
    ("skills add 缺参数", ["skills", "add"], None, "intentional"),
    ("REPL: 启动横幅", [], "/exit\n", "banner_only"),
    ("REPL: /help", [], "/help\n/exit\n", "intentional"),
    ("REPL: /tools", [], "/tools\n/exit\n", "intentional"),
    ("REPL: 普通问答（mock）", [], "你好\n/exit\n", "intentional"),
    ("REPL: /ls .（mock 发工具调用）", [], "/ls .\n/exit\n", "intentional"),
]

#: 有意差异的理由。键是场景标签。
INTENTIONAL: dict[str, str] = {
    "skills add 缺参数": (
        "原版抛未捕获异常，打完整 Node 栈；本实现打一行 `error: …` 并退出 1。"
        "两者都是失败且退出码相同——原版那份栈对用户没有信息量。"
    ),
    "REPL: 启动横幅": (
        "**框体逐字节一致**（这是本场景断言的部分）。框外本实现多打两样："
        "`输入 /help …` 提示行与 `› ` 提示符——行模式 REPL 没有提示符，用户就"
        "不知道在哪输入；原版的 readline 干脆什么都不显示。"
    ),
    "REPL: /help": (
        "命令**集合与顺序完全一致**（37 条，已由 tests/test_slash_commands.py "
        "逐条钉住），差异只在描述文案：原版英文，本实现中文（项目整体本地化）。"
    ),
    "REPL: /tools": (
        "工具集合一致（17 个，已由对等清单钉住），差异在描述语言与「只读」标注。"
    ),
    "REPL: 普通问答（mock）": (
        "mock 模型的回复文案与元信息行（⏱）是本实现自有的；原版 mock 的输出"
        "格式不同。mock 只用于离线自测，不是产品行为。"
    ),
    "REPL: /ls .（mock 发工具调用）": (
        "同上；另加工具结果的行模式排版（⏺ / ⎿）是本实现自有的渲染层。"
    ),
}


def box_lines(text: str) -> list[str]:
    """只取框体行（横幅的 `╭ │ ╰`）。"""
    return [line for line in text.splitlines() if line[:1] in ("╭", "│", "╰")]


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="e2e-"))
    workspace = base / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
    (workspace / "b.txt").write_text("world\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "c.txt").write_text("nested\n", encoding="utf-8")

    ts_home = str(base / "ts-home")
    py_home = str(base / "py-home")
    Path(ts_home).mkdir()
    Path(py_home).mkdir()

    strictly_ok = 0
    strictly_total = 0
    failures: list[str] = []

    for label, args, stdin, expectation in SCENARIOS:
        ts_out = run_ts(args, str(workspace), ts_home, stdin)
        py_out = run_py(args, str(workspace), py_home, stdin)
        ts_norm = normalize(ts_out, ts_home, py_home)
        py_norm = normalize(py_out, py_home, py_home)

        if expectation == "banner_only":
            ts_cmp = "\n".join(box_lines(ts_norm)).strip()
            py_cmp = "\n".join(box_lines(py_norm)).strip()
        else:
            ts_cmp, py_cmp = ts_norm.strip(), py_norm.strip()
        same = ts_cmp == py_cmp

        if expectation in ("identical", "banner_only"):
            strictly_total += 1
            if same:
                strictly_ok += 1
                print(f"OK    {label}")
            else:
                failures.append(label)
                print(f"FAIL  {label}  （要求逐字一致）")
                import difflib
                diff = list(difflib.unified_diff(
                    ts_cmp.splitlines(), py_cmp.splitlines(), "TS", "PY", lineterm=""
                ))
                for line in diff[:20]:
                    print(f"        {line}")
        else:
            reason = INTENTIONAL.get(label, "")
            state = "同" if same else "异"
            print(f"SKIP  {label}  [{state}] {reason[:56]}…")
            if not reason:
                failures.append(f"{label}（有意差异但没写理由）")

    print()
    print(f"逐字一致类：{strictly_ok}/{strictly_total}")
    skipped = len(SCENARIOS) - strictly_total
    print(f"有意差异类：{skipped} 个（已登记理由，不计入失败）")
    if failures:
        print(f"\n失败：{failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

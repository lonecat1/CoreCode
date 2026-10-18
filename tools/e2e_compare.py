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
    # ---- 本地工具快捷方式：**原版只在 TUI 里接** ----
    #
    # `index.ts`（普通 readline 路径）**完全不** import `parseLocalToolShortcut`，
    # 只有 `tty-app.ts:1385`（TUI）用它。所以这些命令在原版的非 TUI 路径下
    # 一律回「未识别命令」。
    #
    # 本实现放弃了 TUI，于是把它们接进了行模式 REPL（已登记的有意差异）。
    # 两边**不可比**：一边不认识、一边真的执行。这类场景用 `py_only`：
    # 不比 stdout，改为断言 Python 自己留下的**文件结果**。
    #
    # 用 `--yes` 让批准自动通过——否则管道 stdin 会让提示直接遇到 EOF 而拒绝，
    # 那就变成了「断言两边都没做事」的空测试。
    ("工具: /ls", [], "/ls .\n/exit\n", "py_only"),
    ("工具: /read", [], "/read a.txt\n/exit\n", "py_only"),
    ("工具: /grep", [], "/grep alpha::.\n/exit\n", "py_only"),
    ("工具: /write 新建", [], "/write new.txt::hello\n/exit\n", "py_only"),
    ("工具: /write 覆盖", [], "/write b.txt::replaced\n/exit\n", "py_only"),
    ("工具: /edit 命中", [], "/edit a.txt::alpha::BETA\n/exit\n", "py_only"),
    ("工具: /edit 未命中", [], "/edit a.txt::nope::x\n/exit\n", "py_only"),
    ("工具: /patch 两处", [], "/patch a.txt::alpha::one::gamma::two\n/exit\n", "py_only"),
    ("工具: /patch 有一处不中", [], "/patch a.txt::alpha::one::zzz::two\n/exit\n", "py_only"),
    ("工具: /cmd", [], "/cmd echo hi\n/exit\n", "py_only"),
    ("工具: /cmd 不在白名单", [], "/cmd curl http://x\n/exit\n", "py_only"),
]

#: `py_only` 场景**期望的文件结果**：`相对路径 -> 内容`。
#:
#: 这些断言是本实现自己的行为契约（因为原版在这条路径上根本没有这个功能）。
#: 只列**被改动**的文件；没列出的文件必须保持出厂的初始内容。
EXPECTED_FILES: dict[str, dict[str, str]] = {
    "工具: /ls": {},
    "工具: /read": {},
    "工具: /grep": {},
    # `/write <path>::<content>` 写入的是**逐字节的 content**，不补换行。
    # 命令里给的是 `hello`，文件里就是 `hello`（不是 `hello\n`）。
    # 这一点与原版一致：`writeFile(path, content, 'utf8')` 写什么就是什么。
    "工具: /write 新建": {"new.txt": "hello"},
    # 覆盖已有文件同理：原来的 `world\n` 被整体换成 `replaced`（**没有**行尾换行）。
    "工具: /write 覆盖": {"b.txt": "replaced"},
    "工具: /edit 命中": {"a.txt": "BETA\nbeta\ngamma\n"},
    # 未命中 → **一个字节都不改**
    "工具: /edit 未命中": {},
    # 两处都命中 → 两处都换掉
    "工具: /patch 两处": {"a.txt": "one\nbeta\ntwo\n"},
    # 有一处不中 → **整体放弃**，文件不动（避免留下半成品）
    "工具: /patch 有一处不中": {},
    # `/cmd` 不写文件
    "工具: /cmd": {},
    "工具: /cmd 不在白名单": {},
}

#: `py_only` 场景给 Python 侧额外的命令行参数。
PY_ONLY_ARGS: list[str] = ["--yes"]

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

#: `py_only` 场景的说明——**原版在这条路径上没有这个功能**。
PY_ONLY_REASON = (
    "**原版只在 TUI 里接这些快捷方式**：`index.ts`（普通 readline 路径）"
    "完全不 import `parseLocalToolShortcut`，只有 `tty-app.ts:1385` 用它。"
    "所以原版对 `/ls` `/read` `/write` `/edit` `/patch` `/cmd` 一律回"
    "「未识别命令」。本实现放弃了 TUI，于是把它们接进行模式 REPL"
    "（已登记的有意差异）。因此这个场景**不可比**——不比 stdout，改为断言"
    "本实现自己留下的**文件结果**（见 EXPECTED_FILES）。"
)


def box_lines(text: str) -> list[str]:
    """只取框体行（横幅的 `╭ │ ╰`）。"""
    return [line for line in text.splitlines() if line[:1] in ("╭", "│", "╰")]


def make_workspace(base: Path) -> Path:
    """造一份**独立的**工作目录。

    必须独立，而且要**两侧各一份**：`/write` `/edit` `/patch` 会真的改文件。
    共用一份的话，TS 跑完 Python 看到的是改过的状态——`/edit a.txt::alpha::…`
    在 Python 那侧会「找不到 alpha」，差异由此产生，但那不是实现差异。
    """
    workspace = base / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (workspace / "b.txt").write_text("world\n", encoding="utf-8")
    (workspace / "sub").mkdir(exist_ok=True)
    (workspace / "sub" / "c.txt").write_text("nested\n", encoding="utf-8")
    return workspace


def snapshot_tree(root: Path, home: str | None = None) -> dict[str, str]:
    """把一份工作目录拍成 `相对路径 -> 内容`（二进制用十六进制）。

    这是**行为**层面的断言依据：`/write` `/edit` `/patch` 的真实契约是
    「文件变成什么样」，而不是终端上打了什么。渲染层可以有差异，文件不能。
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".mini-code/") or rel.startswith(".git/"):
            # 会话/权限等状态文件不属于行为契约，且天然含时间戳。
            continue
        try:
            out[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            out[rel] = path.read_bytes().hex()
    return out


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="e2e-"))
    ts_home = str(base / "ts-home")
    py_home = str(base / "py-home")
    Path(ts_home).mkdir()
    Path(py_home).mkdir()

    strictly_ok = 0
    strictly_total = 0
    behavior_failures: list[str] = []
    failures: list[str] = []

    for label, args, stdin, expectation in SCENARIOS:
        # **每个场景、每一侧各一份独立工作目录。**
        #
        # 两侧不能共用：`/write` `/edit` `/patch` 会真的改文件，TS 先跑完
        # 再让 Python 跑，Python 看到的就是改过的状态——`/edit a.txt::alpha::…`
        # 在那一侧会「找不到 alpha」，差异由此产生，但那不是实现差异。
        slug = f"{abs(hash(label)) % 10**8}"

        if expectation == "py_only":
            # 原版没有这个功能（见 PY_ONLY_REASON），只能断言本实现自己。
            py_ws = make_workspace(base / f"py-ws-{slug}")
            py_out = run_py([*args, *PY_ONLY_ARGS], str(py_ws), py_home, stdin)
            expected = EXPECTED_FILES.get(label)
            actual = snapshot_tree(py_ws)
            if expected is None:
                failures.append(f"{label}（py_only 但没写 EXPECTED_FILES）")
                print(f"FAIL  {label}  （没写期望的文件结果）")
                continue
            # 期望里没列的文件，必须保持出厂内容。
            baseline = snapshot_tree(make_workspace(base / f"base-{slug}"))
            want = {**baseline, **expected}
            if actual != want:
                behavior_failures.append(label)
                print(f"FAIL  {label}  （**文件结果不符**）")
                for name in sorted(set(actual) | set(want)):
                    if actual.get(name) != want.get(name):
                        print(f"        {name}:")
                        print(f"          期望: {want.get(name)!r}")
                        print(f"          实际: {actual.get(name)!r}")
                # 输出里有没有报错也要看一眼，便于定位。
                for line in normalize(py_out, py_home, py_home).splitlines()[-6:]:
                    print(f"          | {line[:100]}")
            else:
                print(f"OK    {label}  （py_only：文件结果符合期望）")
            continue

        ts_ws = make_workspace(base / f"ts-ws-{slug}")
        py_ws = make_workspace(base / f"py-ws-{slug}")
        ts_out = run_ts(args, str(ts_ws), ts_home, stdin)
        py_out = run_py(args, str(py_ws), py_home, stdin)

        # **行为断言：两侧留下的文件必须完全一样。**
        #
        # 这条比 stdout 对比强得多：`/write` `/edit` `/patch` 的真实契约是
        # 「文件变成什么样」。渲染层允许不同，文件不允许。
        ts_tree = snapshot_tree(ts_ws)
        py_tree = snapshot_tree(py_ws)
        if ts_tree != py_tree:
            behavior_failures.append(label)
            print(f"FAIL  {label}  （**文件结果不同**）")
            for name in sorted(set(ts_tree) | set(py_tree)):
                if ts_tree.get(name) != py_tree.get(name):
                    print(f"        {name}:")
                    print(f"          TS: {ts_tree.get(name)!r}")
                    print(f"          PY: {py_tree.get(name)!r}")
            continue

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
    print(f"行为（文件结果）断言：全部 {len(SCENARIOS)} 个场景都查了")
    if behavior_failures:
        print(f"\n**行为不一致**：{behavior_failures}")
        return 1
    if failures:
        print(f"\n失败：{failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
